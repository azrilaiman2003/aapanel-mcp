# coding: utf-8
"""Escape hatches.

aaPanel has hundreds of endpoints and a plugin system on top; no curated tool set covers
all of it. These tools reach the rest. `panel_data_table` is a plain read and lives in the
read tier; the two arbitrary-call tools are in the `raw` tier, which is off unless the
administrator turns it on, because between them they can do anything the panel can do.
"""

from ..panel_client import DATA_TABLES
from ..registry import TIER_RAW, TIER_READ, ToolError
from .common import integer, obj, string

DOMAIN = 'raw'


def register(registry):
    tool = registry.tool

    @tool('panel_data_table',
          'Read one of the panel data tables directly: sites, databases, ftps, crontab, '
          'logs, firewall, tasks, domain.',
          TIER_READ, schema=obj({
              'table': string('Table name.', enum=list(DATA_TABLES)),
              'search': string('Filter rows by name.', default=''),
              'page': integer('Page number, 1-based.', default=1, minimum=1),
              'limit': integer('Rows per page.', default=100, minimum=1, maximum=1000),
          }, required=['table']), domain=DOMAIN, title='Read panel table')
    def panel_data_table(ctx, args):
        return ctx.panel.get_data(args['table'], page=args['page'], limit=args['limit'],
                                  search=args['search'])

    @tool('panel_raw_call',
          'Call any aaPanel HTTP endpoint directly. Use this only for panel features that '
          'have no dedicated tool: the specific tools validate their inputs and describe '
          'their effects, this one does neither. Example: route "/site", action '
          '"GetSecurity", params {"name": "example.com"}.',
          TIER_RAW, schema=obj({
              'route': string('Panel route, starting with a slash, e.g. "/site" or '
                              '"/btdocker/container/get_list".'),
              'action': string('The action parameter, for routes that dispatch on one. '
                               'Leave empty for path-dispatched routes.', default=''),
              'params': {'type': 'object', 'description': 'Request parameters.', 'default': {}},
          }, required=['route']), domain=DOMAIN, title='Raw panel call')
    def panel_raw_call(ctx, args):
        route = args['route']
        if not route.startswith('/'):
            raise ToolError('route must start with a slash, e.g. "/site".')
        params = dict(args.get('params') or {})
        for reserved in ('request_time', 'request_token'):
            params.pop(reserved, None)
        if args['action']:
            params['action'] = args['action']
        return {'route': route, 'action': args['action'] or None,
                'result': ctx.panel.request(route, params)}

    @tool('panel_plugin_call',
          'Call a method on any installed aaPanel plugin, through the panel plugin bridge. '
          'This is how features that live in plugins are reached when they have no dedicated '
          'tool. Use app_list to see what is installed.',
          TIER_RAW, schema=obj({
              'plugin': string('Plugin name, e.g. mail_sys.'),
              'method': string('Method name on the plugin main class.'),
              'params': {'type': 'object', 'description': 'Parameters for the method.',
                         'default': {}},
          }, required=['plugin', 'method']), domain=DOMAIN, title='Raw plugin call')
    def panel_plugin_call(ctx, args):
        if not ctx.panel.plugin_installed(args['plugin']):
            raise ToolError('No plugin named "%s" is installed. Use app_list to see what is.'
                            % args['plugin'])
        params = dict(args.get('params') or {})
        return {'plugin': args['plugin'], 'method': args['method'],
                'result': ctx.panel.plugin_call(args['plugin'], args['method'], **params)}
