# coding: utf-8
"""FTP account tools: the /ftp route and the ftps table."""

from ..registry import TIER_DESTRUCTIVE, TIER_READ, TIER_WRITE
from .common import PAGE_ARGS, boolean, expect, obj, ok, paged, resolve_ftp, string

DOMAIN = 'ftp'
USER_ARG = string('FTP account: its username or its numeric id.')
FTP_FIELDS = ('id', 'name', 'path', 'status', 'ps', 'addtime')


def register(registry):
    tool = registry.tool

    @tool('ftp_list', 'List the FTP accounts on this server.', TIER_READ,
          schema=obj(dict(PAGE_ARGS)), domain=DOMAIN, title='List FTP accounts')
    def ftp_list(ctx, args):
        return paged(ctx.panel, 'ftps', args, FTP_FIELDS)

    @tool('ftp_create', 'Create an FTP account rooted at a directory. The password is '
                        'returned once, so pass it on to the user.',
          TIER_WRITE, schema=obj({
              'username': string('FTP username. Letters, digits and underscores only.',
                                 minLength=3, pattern=r'^\w+$'),
              'path': string('Directory the account is rooted at, e.g. /www/wwwroot/example.com.'),
              'password': string('Password. Generated when omitted.', default=''),
              'remark': string('Note shown in the panel.', default=''),
          }, required=['username', 'path']), domain=DOMAIN, title='Create FTP account')
    def ftp_create(ctx, args):
        password = args['password'] or _password()
        expect(ctx.panel.call('/ftp', 'AddUser', ftp_username=args['username'],
                              ftp_password=password, path=args['path'],
                              ps=args['remark'] or args['username']),
               'Creating the FTP account')
        return ok('FTP account %s created at %s.' % (args['username'], args['path']),
                  username=args['username'], password=password, path=args['path'])

    @tool('ftp_set_password', 'Change the password of an FTP account.', TIER_WRITE,
          schema=obj({
              'user': USER_ARG,
              'password': string('The new password. Generated when omitted.', default=''),
          }, required=['user']), domain=DOMAIN, title='Change FTP password')
    def ftp_set_password(ctx, args):
        row = resolve_ftp(ctx.panel, args['user'])
        password = args['password'] or _password()
        expect(ctx.panel.call('/ftp', 'SetUserPassword', id=row['id'],
                              ftp_username=row['name'], new_password=password),
               'Changing the FTP password')
        return ok('Password changed for %s.' % row['name'], username=row['name'],
                  password=password)

    @tool('ftp_set_status', 'Enable or disable an FTP account.', TIER_WRITE,
          schema=obj({
              'user': USER_ARG,
              'enabled': boolean('True to enable the account, false to disable it.'),
          }, required=['user', 'enabled']), domain=DOMAIN, title='Enable/disable FTP account')
    def ftp_set_status(ctx, args):
        row = resolve_ftp(ctx.panel, args['user'])
        expect(ctx.panel.call('/ftp', 'SetStatus', id=row['id'], username=row['name'],
                              status='1' if args['enabled'] else '0'),
               'Changing the FTP account status')
        return ok('%s is now %s.' % (row['name'], 'enabled' if args['enabled'] else 'disabled'))

    @tool('ftp_delete', 'Delete an FTP account. The files it pointed at are left alone.',
          TIER_DESTRUCTIVE, schema=obj({'user': USER_ARG}, required=['user']),
          domain=DOMAIN, title='Delete FTP account')
    def ftp_delete(ctx, args):
        row = resolve_ftp(ctx.panel, args['user'])
        expect(ctx.panel.call('/ftp', 'DeleteUser', id=row['id'], username=row['name']),
               'Deleting the FTP account')
        return ok('FTP account %s deleted.' % row['name'])


def _password(length=18):
    import secrets
    import string as _string
    alphabet = _string.ascii_letters + _string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))
