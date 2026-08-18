# coding: utf-8
"""Server-level tools: /system, /ajax and /task."""

from ..registry import TIER_DESTRUCTIVE, TIER_READ, TIER_WRITE
from .common import NO_ARGS, expect, integer, obj, ok, rows_of, string

DOMAIN = 'system'

SERVICE_HINT = ('Service name as the panel knows it: nginx, apache, openlitespeed, mysqld, '
                'php-fpm-82 (the version has no dot), redis, memcached, pure-ftpd, tomcat. '
                'Use "webserver" for whichever web server is installed.')


def register(registry):
    tool = registry.tool

    @tool('system_overview', 'Load, CPU, memory, disk and network for the whole server.',
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='Server overview')
    def system_overview(ctx, args):
        return {
            'total': ctx.panel.call('/system', 'GetSystemTotal'),
            'disks': ctx.panel.call('/system', 'GetDiskInfo'),
            'network': ctx.panel.call('/system', 'GetNetWork'),
        }

    @tool('system_version', 'Operating system and panel version.', TIER_READ, schema=NO_ARGS,
          domain=DOMAIN, title='System version')
    def system_version(ctx, args):
        state = ctx.panel.status()
        # GetSystemTotal, not GetSystemVersion: the latter is declared `def
        # GetSystemVersion(self)` on aaPanel 8.0.5 while every action is dispatched as
        # `method(get)`, so it raises before returning. GetSystemTotal carries the same
        # two facts and is callable everywhere.
        total = ctx.panel.call('/system', 'GetSystemTotal')
        if not isinstance(total, dict):
            total = {}
        return {
            'panel_version': total.get('version') or state.get('panel_version'),
            'panel_url': state.get('base_url'),
            'system': total.get('system'),
        }

    @tool('system_processes', 'Running processes, heaviest first.', TIER_READ,
          schema=obj({'limit': integer('How many processes to return.', default=25,
                                       minimum=1, maximum=200)}),
          domain=DOMAIN, title='List processes')
    def system_processes(ctx, args):
        result = ctx.panel.call('/ajax', 'GetProcessList', p=1, limit=args['limit'])
        return {'processes': rows_of(result) or result}

    @tool('system_installed_software', 'Software the panel manages and whether it is running.',
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='Installed software')
    def system_installed_software(ctx, args):
        result = ctx.panel.call('/ajax', 'GetSoftList')
        items = result.get('local', result) if isinstance(result, dict) else result
        trimmed = [
            {k: row.get(k) for k in ('name', 'title', 'version', 'setup', 'status', 'type')
             if k in row}
            for row in rows_of({'data': items} if isinstance(items, list) else items)
        ]
        return {'software': trimmed}

    @tool('system_panel_logs', 'Recent entries from the panel operation log.', TIER_READ,
          schema=obj({'limit': integer('How many entries.', default=50, minimum=1, maximum=500)}),
          domain=DOMAIN, title='Panel operation log')
    def system_panel_logs(ctx, args):
        # /ajax?action=GetOpeLogs reads a log *file* named by a `path` parameter and
        # raises AttributeError without one; it is not the operation-log reader its name
        # suggests. The operation log is a table.
        result = ctx.panel.get_data('logs', page=1, limit=args['limit'])
        return {'logs': rows_of(result) or result}

    @tool('system_tasks', 'Background tasks the panel is running, such as backups and '
                          'installations.',
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='List background tasks')
    def system_tasks(ctx, args):
        return {'tasks': rows_of(ctx.panel.call('/task', 'get_task_lists', p=1, limit=100))}

    @tool('service_restart', 'Restart or reload a service. A reload keeps connections alive '
                             'where the service supports it; a restart drops them briefly.',
          TIER_WRITE, schema=obj({
              'service': string(SERVICE_HINT),
              'mode': string('restart or reload.', enum=['restart', 'reload'], default='reload'),
          }, required=['service']), domain=DOMAIN, title='Restart service')
    def service_restart(ctx, args):
        expect(ctx.panel.call('/system', 'ServiceAdmin', name=args['service'],
                              type=args['mode']), '%sing %s' % (args['mode'].title(), args['service']))
        return ok('%s %sed.' % (args['service'], args['mode']))

    @tool('service_set_state', 'Start or stop a service. Stopping the web server or the '
                               'database takes every site on this server offline.',
          TIER_DESTRUCTIVE, schema=obj({
              'service': string(SERVICE_HINT),
              'state': string('start or stop.', enum=['start', 'stop']),
          }, required=['service', 'state']), domain=DOMAIN, title='Start/stop service')
    def service_set_state(ctx, args):
        expect(ctx.panel.call('/system', 'ServiceAdmin', name=args['service'],
                              type=args['state']),
               '%sing %s' % (args['state'].title(), args['service']))
        past = 'stopped' if args['state'] == 'stop' else 'started'
        return ok('%s %s.' % (args['service'], past))

    @tool('server_reboot', 'Reboot the whole machine. Every service goes down until it comes '
                           'back up, and it may not come back up.',
          TIER_DESTRUCTIVE, schema=NO_ARGS, domain=DOMAIN, title='Reboot server')
    def server_reboot(ctx, args):
        result = ctx.panel.call('/system', 'RestartServer')
        return ok('Reboot requested. The panel and this MCP server will be unreachable for a '
                  'few minutes.', panel_result=result)
