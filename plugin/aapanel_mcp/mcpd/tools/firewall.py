# coding: utf-8
"""Firewall tools: the /firewall route (iptables, firewalld or ufw, whichever the host uses)."""

from ..registry import TIER_DESTRUCTIVE, TIER_READ, TIER_WRITE, ToolError
from .common import NO_ARGS, expect, integer, obj, ok, rows_of, string

DOMAIN = 'firewall'


def register(registry):
    tool = registry.tool

    @tool('firewall_list', 'Open ports and blocked addresses.', TIER_READ, schema=NO_ARGS,
          domain=DOMAIN, title='List firewall rules')
    def firewall_list(ctx, args):
        # Read the table rather than call /firewall?action=GetList. On aaPanel 8.0.5
        # that method is declared `def GetList(self)` while the dispatcher invokes every
        # action as `method(get)`, so it raises TypeError before it runs — the route is
        # simply not callable there. The table it would have read is right here.
        result = ctx.panel.get_data('firewall', page=1, limit=500)
        return {'rules': rows_of(result) or result}

    @tool('firewall_ssh_info', 'SSH port, root login and password login settings.', TIER_READ,
          schema=NO_ARGS, domain=DOMAIN, title='SSH settings')
    def firewall_ssh_info(ctx, args):
        return ctx.panel.call('/firewall', 'GetSshInfo')

    @tool('firewall_open_port', 'Open a port in the firewall.', TIER_WRITE,
          schema=obj({
              'port': string('Port or range, e.g. "8080" or "9000:9100".'),
              'remark': string('Note shown in the panel.', default=''),
              'protocol': string('Protocol to allow.', enum=['tcp', 'udp', 'all'], default='tcp'),
          }, required=['port']), domain=DOMAIN, title='Open port')
    def firewall_open_port(ctx, args):
        expect(ctx.panel.call('/firewall', 'AddAcceptPort', port=args['port'],
                              ps=args['remark'] or 'opened by aapanel-mcp',
                              type=args['protocol']), 'Opening the port')
        return ok('Port %s is open (%s).' % (args['port'], args['protocol']))

    @tool('firewall_block_ip', 'Block an IP address or range at the firewall.', TIER_WRITE,
          schema=obj({
              'address': string('Address or CIDR range to block.'),
              'remark': string('Note shown in the panel.', default=''),
          }, required=['address']), domain=DOMAIN, title='Block address')
    def firewall_block_ip(ctx, args):
        # The panel's own parameter for the address on this endpoint is called "port".
        expect(ctx.panel.call('/firewall', 'AddDropAddress', port=args['address'],
                              ps=args['remark'] or 'blocked by aapanel-mcp'),
               'Blocking the address')
        return ok('%s is blocked.' % args['address'])

    @tool('firewall_unblock_ip', 'Remove an address from the block list.', TIER_WRITE,
          schema=obj({'address': string('The blocked address to release.')},
                     required=['address']), domain=DOMAIN, title='Unblock address')
    def firewall_unblock_ip(ctx, args):
        rule = _find_rule(ctx.panel, args['address'])
        expect(ctx.panel.call('/firewall', 'DelDropAddress', port=args['address'],
                              id=rule.get('id', '')), 'Unblocking the address')
        return ok('%s is no longer blocked.' % args['address'])

    @tool('firewall_close_port', 'Close a port in the firewall. Anything currently served '
                                 'on it becomes unreachable.',
          TIER_DESTRUCTIVE, schema=obj({
              'port': string('The port or range to close.'),
              'protocol': string('Protocol the rule was added for.',
                                 enum=['tcp', 'udp', 'all'], default='tcp'),
          }, required=['port']), domain=DOMAIN, title='Close port')
    def firewall_close_port(ctx, args):
        rule = _find_rule(ctx.panel, args['port'])
        expect(ctx.panel.call('/firewall', 'DelAcceptPort', port=args['port'],
                              id=rule.get('id', ''), type=args['protocol']),
               'Closing the port')
        return ok('Port %s is closed.' % args['port'])

    @tool('firewall_set_ssh_port',
          'Change the SSH port. Opening the new port in the firewall first is on you: get '
          'this wrong and you lock everyone out of the server.',
          TIER_DESTRUCTIVE, schema=obj({
              'port': integer('The new SSH port.', minimum=1, maximum=65535),
          }, required=['port']), domain=DOMAIN, title='Change SSH port')
    def firewall_set_ssh_port(ctx, args):
        expect(ctx.panel.call('/firewall', 'SetSshPort', port=args['port']),
               'Changing the SSH port')
        return ok('SSH now listens on port %s. Confirm you can still log in before closing '
                  'the old port.' % args['port'])


def _find_rule(panel, needle):
    """Rules are deleted by id; look it up from the list so callers only need the value."""
    try:
        rows = rows_of(panel.get_data('firewall', page=1, limit=500))
    except ToolError:
        return {}
    for row in rows:
        if str(row.get('port', '')) == str(needle):
            return row
    return {}
