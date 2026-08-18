# coding: utf-8
"""Installed application tools: the /plugin route and the plugin directory."""

import json
import os

from ..config import panel_home
from ..registry import TIER_DESTRUCTIVE, TIER_READ, TIER_WRITE, ToolError
from .common import NO_ARGS, expect, obj, ok, rows_of, string

DOMAIN = 'apps'


def register(registry):
    tool = registry.tool

    @tool('app_list', 'Applications installed on this panel: web servers, PHP versions, '
                      'databases, the mail server, Docker manager, and third-party plugins.',
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='List installed apps')
    def app_list(ctx, args):
        apps = []
        root = os.path.join(panel_home(), 'plugin')
        for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            info_path = os.path.join(root, name, 'info.json')
            if not os.path.isfile(info_path):
                continue
            try:
                with open(info_path, encoding='utf-8-sig') as fp:
                    info = json.load(fp)
            except (ValueError, OSError):
                info = {}
            apps.append({
                'name': name,
                'title': info.get('title', name),
                'version': info.get('versions'),
                'description': info.get('ps', ''),
            })
        return {'count': len(apps), 'apps': apps}

    @tool('app_store_list', 'Applications available to install from the aaPanel app store.',
          TIER_READ, schema=obj({
              'search': string('Filter by name or title.', default=''),
          }), domain=DOMAIN, title='List app store')
    def app_store_list(ctx, args):
        result = ctx.panel.call('/plugin', 'get_soft_list', type=0, p=1, row=200,
                                query=args['search'])
        items = result.get('list', result) if isinstance(result, dict) else result
        rows = rows_of({'data': items} if isinstance(items, list) else items)
        trimmed = [{k: row.get(k) for k in ('name', 'title', 'version', 'ps', 'setup', 'type')
                    if k in row} for row in rows]
        if args['search']:
            needle = args['search'].lower()
            trimmed = [row for row in trimmed
                       if needle in str(row.get('name', '')).lower()
                       or needle in str(row.get('title', '')).lower()]
        return {'count': len(trimmed), 'apps': trimmed}

    @tool('app_status', 'Whether an installed app is running, and its version.', TIER_READ,
          schema=obj({'app': string('App name, e.g. nginx, mysql, mail_sys.')},
                     required=['app']), domain=DOMAIN, title='App status')
    def app_status(ctx, args):
        return {
            'app': args['app'],
            'status': ctx.panel.call('/plugin', 'getPluginStatus', name=args['app']),
        }

    @tool('app_install', 'Install an application from the app store. Large installs run in '
                         'the background; check system_tasks for progress.',
          TIER_WRITE, schema=obj({
              'app': string('App name from app_store_list.'),
              'version': string('Version to install. The default is the newest.', default=''),
          }, required=['app']), domain=DOMAIN, title='Install app')
    def app_install(ctx, args):
        params = {'sName': args['app'], 'type': 1}
        if args['version']:
            params['version'] = args['version']
        result = expect(ctx.panel.call('/plugin', 'install_plugin', **params),
                        'Installing the app')
        return ok('Installation of %s started.' % args['app'], panel_result=result)

    @tool('app_uninstall', 'Uninstall an application. Its data and configuration usually go '
                           'with it.',
          TIER_DESTRUCTIVE, schema=obj({
              'app': string('App name from app_list.'),
              'version': string('Version to uninstall, for apps with several installed.',
                                default=''),
          }, required=['app']), domain=DOMAIN, title='Uninstall app')
    def app_uninstall(ctx, args):
        if args['app'] == 'aapanel_mcp':
            raise ToolError('Refusing to uninstall the plugin that is serving this request. '
                            'Do that from the panel if you really mean to.')
        params = {'sName': args['app'], 'type': 1}
        if args['version']:
            params['version'] = args['version']
        result = expect(ctx.panel.call('/plugin', 'uninstall_plugin', **params),
                        'Uninstalling the app')
        return ok('Uninstall of %s started.' % args['app'], panel_result=result)
