# coding: utf-8
"""Mail server tools.

aaPanel has no /mail route: the mail server is a plugin, reached through
`/plugin?action=a&name=mail_sys&s=<method>`. Which methods exist depends on the plugin
version installed, and there is no published list — so instead of shipping guesses that
break on the next release, these tools **discover** the installed plugin's callable
methods and the parameters each one reads, straight from its source on disk.

`mail_capabilities` is the entry point: call it first, then use mail_read / mail_manage /
mail_delete. The three are split by verb so the permission tiers still mean something —
a read-only server can query the mail system but cannot change it.
"""

import os
import re

from ..config import panel_home
from ..registry import TIER_DESTRUCTIVE, TIER_READ, TIER_WRITE, ToolError
from .common import NO_ARGS, obj, string

DOMAIN = 'mail'

MAIL_PLUGINS = ('mail_sys', 'billionmail')
READ_PREFIXES = ('get_', 'list_', 'check_', 'is_', 'search_', 'query_', 'test_', 'find_')
DELETE_WORDS = ('delete', 'remove', 'del_', 'drop', 'clear', 'destroy')


def _plugin_dir(name):
    return os.path.join(panel_home(), 'plugin', name)


def installed_mail_plugin(panel=None):
    for name in MAIL_PLUGINS:
        if os.path.isfile(os.path.join(_plugin_dir(name), 'info.json')):
            return name
    return None


def _mail_available(panel):
    return installed_mail_plugin(panel) is not None


def discover_methods(plugin):
    """Public methods of a plugin's main class, with the parameters each one reads.

    Parsed rather than imported: importing a panel plugin outside the panel process runs
    its module-level code, which is not something to do casually.
    """
    path = os.path.join(_plugin_dir(plugin), '%s_main.py' % plugin)
    try:
        with open(path, encoding='utf-8', errors='replace') as fp:
            source = fp.read()
    except OSError:
        return {}

    lines = source.split('\n')
    methods = {}
    starts = []
    for index, line in enumerate(lines):
        match = re.match(r'\s*def ([a-zA-Z]\w*)\s*\(\s*self\s*,\s*(get|args|params)\b', line)
        if match:
            starts.append((index, match.group(1)))
    for position, (index, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        body = '\n'.join(lines[index:end])
        found = set(re.findall(r"(?:get|args|params)\.([a-zA-Z_]\w*)", body))
        found |= set(re.findall(r"(?:get|args|params)\[['\"](\w+)['\"]\]", body))
        found -= {'get', 'args', 'params', 'validate', 'keys', 'items', 'action', 'name', 's'}
        methods[name] = sorted(found)
    return methods


def classify(method):
    lowered = method.lower()
    if any(word in lowered for word in DELETE_WORDS):
        return TIER_DESTRUCTIVE
    if lowered.startswith(READ_PREFIXES):
        return TIER_READ
    return TIER_WRITE


def register(registry):
    tool = registry.tool

    @tool('mail_status', 'Which mail stack is installed on this server and whether it is '
                         'reachable.',
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='Mail server status',
          available=_mail_available)
    def mail_status(ctx, args):
        plugin = installed_mail_plugin(ctx.panel)
        methods = discover_methods(plugin)
        return {
            'plugin': plugin,
            'method_count': len(methods),
            'hint': 'Call mail_capabilities to see the methods and their parameters.',
        }

    @tool('mail_capabilities',
          'List what the installed mail plugin can do: every callable method and the '
          'parameters it reads, grouped by whether it reads, changes or deletes. Call this '
          'before mail_read, mail_manage or mail_delete.',
          TIER_READ, schema=obj({
              'filter': string('Only show methods whose name contains this text.', default=''),
          }), domain=DOMAIN, title='Mail capabilities', available=_mail_available)
    def mail_capabilities(ctx, args):
        plugin = installed_mail_plugin(ctx.panel)
        methods = discover_methods(plugin)
        if not methods:
            raise ToolError('Could not read the method list of the %s plugin. Its main module '
                            'may be compiled rather than plain Python; use panel_plugin_call '
                            'if you know the method name.' % plugin)
        grouped = {TIER_READ: {}, TIER_WRITE: {}, TIER_DESTRUCTIVE: {}}
        for name, params in methods.items():
            if args['filter'] and args['filter'].lower() not in name.lower():
                continue
            grouped[classify(name)][name] = params
        return {
            'plugin': plugin,
            'reads': grouped[TIER_READ],
            'changes': grouped[TIER_WRITE],
            'deletes': grouped[TIER_DESTRUCTIVE],
        }

    @tool('mail_read', 'Call a read-only method of the mail plugin, such as listing domains '
                       'or mailboxes. Method names come from mail_capabilities.',
          TIER_READ, schema=obj({
              'method': string('Method name from the "reads" group of mail_capabilities.'),
              'params': {'type': 'object', 'description': 'Parameters for the method.',
                         'default': {}},
          }, required=['method']), domain=DOMAIN, title='Query mail server',
          available=_mail_available)
    def mail_read(ctx, args):
        return _call(ctx, args, allowed=(TIER_READ,))

    @tool('mail_manage', 'Call a method of the mail plugin that changes something: add a '
                         'domain, create a mailbox, set a password, change a forward rule.',
          TIER_WRITE, schema=obj({
              'method': string('Method name from the "changes" group of mail_capabilities.'),
              'params': {'type': 'object', 'description': 'Parameters for the method.',
                         'default': {}},
          }, required=['method']), domain=DOMAIN, title='Manage mail server',
          available=_mail_available)
    def mail_manage(ctx, args):
        return _call(ctx, args, allowed=(TIER_READ, TIER_WRITE))

    @tool('mail_delete', 'Call a method of the mail plugin that deletes something: a domain, '
                         'a mailbox, a forward rule. Mail data removed this way is gone.',
          TIER_DESTRUCTIVE, schema=obj({
              'method': string('Method name from the "deletes" group of mail_capabilities.'),
              'params': {'type': 'object', 'description': 'Parameters for the method.',
                         'default': {}},
          }, required=['method']), domain=DOMAIN, title='Delete from mail server',
          available=_mail_available)
    def mail_delete(ctx, args):
        return _call(ctx, args, allowed=(TIER_READ, TIER_WRITE, TIER_DESTRUCTIVE))


def _call(ctx, args, allowed):
    plugin = installed_mail_plugin(ctx.panel)
    if not plugin:
        raise ToolError('No mail server plugin is installed on this panel.')
    method = args['method']
    methods = discover_methods(plugin)
    if methods and method not in methods:
        raise ToolError('The %s plugin has no method "%s". Call mail_capabilities for the list.'
                        % (plugin, method))
    tier = classify(method)
    if tier not in allowed:
        wanted = {TIER_READ: 'mail_read', TIER_WRITE: 'mail_manage',
                  TIER_DESTRUCTIVE: 'mail_delete'}[tier]
        raise ToolError('"%s" is a %s operation; call it through %s instead.'
                        % (method, tier, wanted))
    params = args.get('params') or {}
    if not isinstance(params, dict):
        raise ToolError('params must be an object.')
    return {'plugin': plugin, 'method': method,
            'result': ctx.panel.plugin_call(plugin, method, **params)}
