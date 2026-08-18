# coding: utf-8
"""Database tools: the /database route and the databases table."""

from ..registry import TIER_DESTRUCTIVE, TIER_READ, TIER_WRITE
from .common import NO_ARGS, PAGE_ARGS, expect, obj, ok, paged, resolve_database, string

DOMAIN = 'databases'
DB_ARG = string('Database: its name or its numeric id.')
DB_FIELDS = ('id', 'name', 'username', 'ps', 'addtime', 'accept', 'sid', 'db_type')
# Only database_info returns these; the listing deliberately leaves the password out.
DETAIL_FIELDS = ('username', 'password', 'accept', 'conn_config', 'quota', 'db_type')


def register(registry):
    tool = registry.tool

    @tool('database_list', 'List the databases on this server.', TIER_READ,
          schema=obj(dict(PAGE_ARGS)), domain=DOMAIN, title='List databases')
    def database_list(ctx, args):
        return paged(ctx.panel, 'databases', args, DB_FIELDS)

    @tool('database_info', 'Connection details for one database, including its password.',
          TIER_READ, schema=obj({'database': DB_ARG}, required=['database']),
          domain=DOMAIN, title='Database details')
    def database_info(ctx, args):
        # Everything here comes from the databases table. /database?action=GetdataInfo is
        # in the route whitelist but no such method exists on the class (aaPanel 8.0.5),
        # so calling it raises AttributeError and the panel answers 404 — and GetInfo is
        # only a one-line wrapper around the same missing method.
        row = resolve_database(ctx.panel, args['database'])
        return {
            'database': {k: row.get(k) for k in DB_FIELDS if k in row},
            'connection': {k: row.get(k) for k in DETAIL_FIELDS if k in row},
        }

    @tool('database_server_status', 'MySQL service status and performance counters.',
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='Database server status')
    def database_server_status(ctx, args):
        return {
            'status': ctx.panel.call('/database', 'GetRunStatus'),
            'info': ctx.panel.call('/database', 'GetMySQLInfo'),
        }

    @tool('database_slow_log', 'The MySQL slow query log.', TIER_READ, schema=NO_ARGS,
          domain=DOMAIN, title='Slow query log')
    def database_slow_log(ctx, args):
        return ctx.panel.call('/database', 'GetSlowLogs')

    @tool('database_create', 'Create a MySQL database with its own user. The password is '
                             'returned once, so pass it on to the user.',
          TIER_WRITE, schema=obj({
              'name': string('Database name. Also used as the username unless one is given.',
                             maxLength=64),
              'username': string('Database username. Defaults to the database name.', default=''),
              'password': string('Password. Generated when omitted.', default=''),
              'access': string('Who may connect: 127.0.0.1 for local only, % for anywhere, '
                               'or a specific address.', default='127.0.0.1'),
              'charset': string('Character set.', default='utf8mb4',
                                enum=['utf8', 'utf8mb4', 'gbk', 'big5', 'latin1']),
              'remark': string('Note shown in the panel.', default=''),
          }, required=['name']), domain=DOMAIN, title='Create database')
    def database_create(ctx, args):
        password = args['password'] or _password()
        username = args['username'] or args['name']
        expect(ctx.panel.call('/database', 'AddDatabase',
                              name=args['name'], db_user=username, password=password,
                              address=args['access'], codeing=args['charset'],
                              ps=args['remark'] or args['name'], sid=0, dtype='MySQL'),
               'Creating the database')
        return ok('Database %s created.' % args['name'], name=args['name'], username=username,
                  password=password, access=args['access'])

    @tool('database_set_password', 'Change the password of a database user.', TIER_WRITE,
          schema=obj({
              'database': DB_ARG,
              'password': string('The new password. Generated when omitted.', default=''),
          }, required=['database']), domain=DOMAIN, title='Change database password')
    def database_set_password(ctx, args):
        row = resolve_database(ctx.panel, args['database'])
        password = args['password'] or _password()
        expect(ctx.panel.call('/database', 'ResDatabasePassword', id=row['id'],
                              name=row['name'], password=password), 'Changing the password')
        return ok('Password changed for %s. Update any application that connects to it.'
                  % row['name'], name=row['name'], password=password)

    @tool('database_set_access', 'Change which hosts may connect to a database.', TIER_WRITE,
          schema=obj({
              'database': DB_ARG,
              'access': string('127.0.0.1 for local only, % for anywhere, or a specific address.'),
          }, required=['database', 'access']), domain=DOMAIN, title='Set database access')
    def database_set_access(ctx, args):
        row = resolve_database(ctx.panel, args['database'])
        expect(ctx.panel.call('/database', 'SetDatabaseAccess', id=row['id'], name=row['name'],
                              access=args['access']), 'Changing database access')
        return ok('%s now accepts connections from %s.' % (row['name'], args['access']))

    @tool('database_backup', 'Back up a database to the panel backup directory.', TIER_WRITE,
          schema=obj({'database': DB_ARG}, required=['database']), domain=DOMAIN,
          title='Back up database')
    def database_backup(ctx, args):
        row = resolve_database(ctx.panel, args['database'])
        result = expect(ctx.panel.call('/database', 'ToBackup', id=row['id']),
                        'Backing up the database')
        return ok('Backup of %s started.' % row['name'], panel_result=result)

    @tool('database_import_sql', 'Import a .sql file that is already on the server into a '
                                 'database. Existing tables with the same names are replaced.',
          TIER_DESTRUCTIVE, schema=obj({
              'database': DB_ARG,
              'sql_file': string('Absolute path of the .sql file on the server.'),
          }, required=['database', 'sql_file']), domain=DOMAIN, title='Import SQL file')
    def database_import_sql(ctx, args):
        row = resolve_database(ctx.panel, args['database'])
        result = expect(ctx.panel.call('/database', 'InputSql', file=args['sql_file'],
                                       name=row['name']), 'Importing the SQL file')
        return ok('Import into %s started.' % row['name'], panel_result=result)

    @tool('database_delete', 'Delete a database and its user. All of its data is lost.',
          TIER_DESTRUCTIVE, schema=obj({'database': DB_ARG}, required=['database']),
          domain=DOMAIN, title='Delete database')
    def database_delete(ctx, args):
        row = resolve_database(ctx.panel, args['database'])
        expect(ctx.panel.call('/database', 'DeleteDatabase', id=row['id'], name=row['name']),
               'Deleting the database')
        return ok('Database %s deleted.' % row['name'])

    @tool('database_delete_backup', 'Delete a stored database backup.', TIER_DESTRUCTIVE,
          schema=obj({'backup_id': string('Backup id from the panel backup list.')},
                     required=['backup_id']), domain=DOMAIN, title='Delete database backup')
    def database_delete_backup(ctx, args):
        expect(ctx.panel.call('/database', 'DelBackup', id=args['backup_id']),
               'Deleting the backup')
        return ok('Backup %s deleted.' % args['backup_id'])


def _password(length=18):
    import secrets
    import string as _string
    alphabet = _string.ascii_letters + _string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))
