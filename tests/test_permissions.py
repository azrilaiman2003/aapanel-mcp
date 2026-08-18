# coding: utf-8
"""Permission tiers and the confirmation handshake."""

import unittest

from support import FakePanel, basic_routes, build_test_server

from mcpd import config as cfg
from mcpd import permissions
from mcpd.protocol import ERA_MODERN, RequestContext
from mcpd.registry import TIER_DESTRUCTIVE, Registry, Tool


def _tool(name='thing_delete', tier=TIER_DESTRUCTIVE):
    return Tool(name=name, handler=lambda ctx, args: {}, description='x',
                input_schema={'type': 'object'}, tier=tier)


class ConfirmTokenTest(unittest.TestCase):
    def setUp(self):
        self.config = {'confirm': {'required': True, 'tiers': ['destructive', 'shell'],
                                   'ttl_seconds': 300, 'secret': 'test-secret'}}

    def test_round_trip(self):
        args = {'site': 'example.com', 'delete_files': True}
        token = permissions.issue_token(self.config, 'site_delete', args)
        self.assertTrue(permissions.verify_token(self.config, 'site_delete', args, token))

    def test_token_does_not_transfer_to_other_arguments(self):
        token = permissions.issue_token(self.config, 'site_delete', {'site': 'a.com'})
        self.assertFalse(permissions.verify_token(self.config, 'site_delete',
                                                  {'site': 'b.com'}, token))

    def test_token_does_not_transfer_to_another_tool(self):
        token = permissions.issue_token(self.config, 'site_delete', {'site': 'a.com'})
        self.assertFalse(permissions.verify_token(self.config, 'database_delete',
                                                  {'site': 'a.com'}, token))

    def test_the_confirm_field_itself_is_not_part_of_the_signature(self):
        args = {'site': 'a.com'}
        token = permissions.issue_token(self.config, 'site_delete', args)
        echoed = dict(args, confirm=token)
        self.assertTrue(permissions.verify_token(self.config, 'site_delete', echoed, token))

    def test_expired_token_is_rejected(self):
        token = permissions.issue_token(self.config, 'site_delete', {}, now=1000)
        self.assertFalse(permissions.verify_token(self.config, 'site_delete', {}, token,
                                                  now=1000 + 301))

    def test_tampered_signature_is_rejected(self):
        token = permissions.issue_token(self.config, 'site_delete', {})
        expiry, _, signature = token.partition('.')
        forged = '%s.%s' % (expiry, 'f' * len(signature))
        self.assertFalse(permissions.verify_token(self.config, 'site_delete', {}, forged))

    def test_extended_expiry_does_not_survive(self):
        token = permissions.issue_token(self.config, 'site_delete', {}, now=1000)
        _, _, signature = token.partition('.')
        self.assertFalse(permissions.verify_token(self.config, 'site_delete', {},
                                                  '99999999999.%s' % signature))

    def test_malformed_tokens_are_rejected(self):
        for bad in ('', None, 'no-dot', '12345.', 'abc.def', 12345):
            self.assertFalse(permissions.verify_token(self.config, 't', {}, bad))


class TierTest(unittest.TestCase):
    def setUp(self):
        self.config = {'permissions': {'tiers': {'read': True, 'write': False,
                                                 'destructive': False, 'shell': False,
                                                 'raw': False},
                                       'tools': {}}}

    def test_tier_default(self):
        self.assertTrue(permissions.tool_enabled(self.config, _tool(tier='read')))
        self.assertFalse(permissions.tool_enabled(self.config, _tool(tier='write')))

    def test_per_tool_override_can_enable_one_tool_of_a_disabled_tier(self):
        self.config['permissions']['tools']['site_create'] = True
        self.assertTrue(permissions.tool_enabled(self.config,
                                                 _tool('site_create', tier='write')))

    def test_per_tool_override_can_disable_one_tool_of_an_enabled_tier(self):
        self.config['permissions']['tools']['site_list'] = False
        self.assertFalse(permissions.tool_enabled(self.config, _tool('site_list', tier='read')))

    def test_visible_tools_skips_unavailable_ones(self):
        registry = Registry()
        registry.add(Tool('here', lambda c, a: {}, 'x', {'type': 'object'}, 'read'))
        registry.add(Tool('gone', lambda c, a: {}, 'x', {'type': 'object'}, 'read',
                          available=lambda panel: False))
        names = [t.name for t in permissions.visible_tools(self.config, registry, panel=object())]
        self.assertEqual(names, ['here'])


class ConfirmFlowTest(unittest.TestCase):
    """The two-step handshake as an agent actually experiences it."""

    def setUp(self):
        self.panel = FakePanel()
        basic_routes(self.panel)
        self.panel.route('/site', 'DeleteSite', {'status': True, 'msg': 'deleted'})
        self.server = build_test_server({'destructive': True})
        self.ctx = RequestContext(ERA_MODERN)

    def tearDown(self):
        self.panel.stop()

    def _call(self, arguments):
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'site_delete', 'arguments': arguments}}
        return self.server.dispatch(message, self.ctx)['result']

    def test_first_call_refuses_and_hands_back_a_token(self):
        result = self._call({'site': 'example.com'})
        self.assertTrue(result['isError'])
        text = result['content'][0]['text']
        self.assertIn('needs confirmation', text)
        self.assertIn('confirm=', text)
        self.assertIn('example.com', text)
        # Nothing reached the panel: the refusal happens before the site is even resolved.
        self.assertEqual(self.panel.calls, [])

    def test_second_call_with_the_token_goes_through(self):
        first = self._call({'site': 'example.com'})
        token = _token_from(first)
        result = self._call({'site': 'example.com', 'confirm': token})
        self.assertFalse(result['isError'], result['content'][0]['text'])
        self.assertEqual(self.panel.calls[-1]['params']['action'], 'DeleteSite')

    def test_a_token_issued_for_a_gentler_call_does_not_authorise_a_harsher_one(self):
        first = self._call({'site': 'example.com'})
        token = _token_from(first)
        result = self._call({'site': 'example.com', 'delete_files': True, 'confirm': token})
        self.assertTrue(result['isError'])
        self.assertIn('invalid or has expired', result['content'][0]['text'])

    def test_confirmation_can_be_switched_off(self):
        config = cfg.load()
        config['confirm']['required'] = False
        cfg.save(config)
        result = self._call({'site': 'example.com'})
        self.assertFalse(result['isError'], result['content'][0]['text'])


def _token_from(result):
    text = result['content'][0]['text']
    marker = 'confirm="'
    start = text.index(marker) + len(marker)
    return text[start:text.index('"', start)]


if __name__ == '__main__':
    unittest.main()
