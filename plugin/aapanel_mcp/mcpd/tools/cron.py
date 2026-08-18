# coding: utf-8
"""Scheduled task tools: the /crontab route."""

from ..registry import TIER_DESTRUCTIVE, TIER_READ, TIER_WRITE, ToolError
from .common import NO_ARGS, expect, integer, obj, ok, resolve_cron, rows_of, string

DOMAIN = 'cron'
TASK_ARG = string('Task: its name or its numeric id.')

# sType values the panel understands. Anything it does not recognise is run as a shell
# script built from sBody, which is what "shell" relies on.
TASK_KINDS = {
    'shell': 'toShell',
    'url': 'toUrl',
    'backup_site': 'site',
    'backup_database': 'database',
    'backup_path': 'path',
    'backup_logs': 'logs',
}


def register(registry):
    tool = registry.tool

    @tool('cron_list', 'List the scheduled tasks, with their schedule and last run.',
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='List cron tasks')
    def cron_list(ctx, args):
        rows = rows_of(ctx.panel.call('/crontab', 'GetCrontab'))
        return {'count': len(rows), 'tasks': rows}

    @tool('cron_logs', 'Output from the last runs of a scheduled task.', TIER_READ,
          schema=obj({'task': TASK_ARG}, required=['task']), domain=DOMAIN,
          title='Cron task log')
    def cron_logs(ctx, args):
        row = resolve_cron(ctx.panel, args['task'])
        return {'task': row.get('name'),
                'log': ctx.panel.call('/crontab', 'GetLogs', id=row['id'])}

    @tool('cron_create',
          'Create a scheduled task: a shell script, a URL to fetch, or a backup of a '
          'website, database, directory or log.',
          TIER_WRITE, schema=obj({
              'name': string('Task name shown in the panel.'),
              'kind': string('What the task does.', enum=sorted(TASK_KINDS), default='shell'),
              'script': string('Shell script body. Required when kind is "shell".', default=''),
              'url': string('URL to fetch. Required when kind is "url".', default=''),
              'target': string('What to back up: the website name, database name or '
                               'directory path. Required for the backup kinds.', default=''),
              'schedule': string('How often to run.',
                                 enum=['day', 'day-n', 'hour', 'hour-n', 'minute-n', 'week', 'month'],
                                 default='day'),
              'hour': integer('Hour of the run, 0-23.', default=0, minimum=0, maximum=23),
              'minute': integer('Minute of the run, 0-59.', default=0, minimum=0, maximum=59),
              'interval': integer('Interval for the "-n" schedules: every N days, hours or '
                                  'minutes.', default=1, minimum=1),
              'weekday': integer('Day of the week for the "week" schedule, 0 is Sunday.',
                                 default=0, minimum=0, maximum=6),
              'day_of_month': integer('Day of the month for the "month" schedule.',
                                      default=1, minimum=1, maximum=31),
              'keep': integer('How many backups to keep, for the backup kinds.', default=3,
                              minimum=1),
              'backup_to': string('Backup destination: "localhost" or the name of a storage '
                                  'plugin.', default='localhost'),
          }, required=['name']), domain=DOMAIN, title='Create cron task')
    def cron_create(ctx, args):
        kind = args['kind']
        stype = TASK_KINDS[kind]
        if kind == 'shell' and not args['script']:
            raise ToolError('kind "shell" needs a script.')
        if kind == 'url' and not args['url']:
            raise ToolError('kind "url" needs a url.')
        if kind.startswith('backup_') and not args['target']:
            raise ToolError('kind "%s" needs a target (website, database or path).' % kind)

        where1 = ''
        if args['schedule'] in ('day-n', 'hour-n', 'minute-n'):
            where1 = args['interval']
        elif args['schedule'] == 'week':
            where1 = args['weekday']
        elif args['schedule'] == 'month':
            where1 = args['day_of_month']

        # The panel reads every one of these keys unconditionally while building the
        # task's shell script, so all of them must be present even when unused.
        params = {
            'name': args['name'],
            'type': args['schedule'],
            'where1': where1,
            'week': args['weekday'],
            'hour': args['hour'],
            'minute': args['minute'],
            'save': args['keep'],
            'backupTo': args['backup_to'],
            'sType': stype,
            'sName': args['target'],
            'sBody': args['script'],
            'urladdress': args['url'],
            'save_local': 0,
            'notice': 0,
            'notice_channel': '',
        }
        result = expect(ctx.panel.call('/crontab', 'AddCrontab', **params), 'Creating the task')
        return ok('Scheduled task "%s" created.' % args['name'], panel_result=result)

    @tool('cron_run_now', 'Run a scheduled task immediately, without changing its schedule.',
          TIER_WRITE, schema=obj({'task': TASK_ARG}, required=['task']), domain=DOMAIN,
          title='Run cron task now')
    def cron_run_now(ctx, args):
        row = resolve_cron(ctx.panel, args['task'])
        result = expect(ctx.panel.call('/crontab', 'StartTask', id=row['id']), 'Running the task')
        return ok('Task "%s" started.' % row.get('name'), panel_result=result)

    @tool('cron_set_status', 'Enable or disable a scheduled task.', TIER_WRITE,
          schema=obj({
              'task': TASK_ARG,
              'enabled': string('"1" to enable, "0" to disable.', enum=['0', '1']),
          }, required=['task', 'enabled']), domain=DOMAIN, title='Enable/disable cron task')
    def cron_set_status(ctx, args):
        row = resolve_cron(ctx.panel, args['task'])
        expect(ctx.panel.call('/crontab', 'set_cron_status', id=row['id'],
                              status=args['enabled']), 'Changing the task status')
        return ok('Task "%s" is now %s.'
                  % (row.get('name'), 'enabled' if args['enabled'] == '1' else 'disabled'))

    @tool('cron_delete', 'Delete a scheduled task.', TIER_DESTRUCTIVE,
          schema=obj({'task': TASK_ARG}, required=['task']), domain=DOMAIN,
          title='Delete cron task')
    def cron_delete(ctx, args):
        row = resolve_cron(ctx.panel, args['task'])
        expect(ctx.panel.call('/crontab', 'DelCrontab', id=row['id']), 'Deleting the task')
        return ok('Task "%s" deleted.' % row.get('name'))
