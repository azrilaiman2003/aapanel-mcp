# coding: utf-8
"""MCP dispatch: both protocol eras, error shapes, and tool-call plumbing."""

import unittest

from support import FakePanel, basic_routes, build_test_server

from mcpd import PROTOCOL_MODERN, SUPPORTED_PROTOCOLS
from mcpd.protocol import (ERA_LEGACY, ERA_MODERN, CODE_INVALID_PARAMS,
                           CODE_METHOD_NOT_FOUND, CODE_UNSUPPORTED_VERSION, McpServer,
                           ProtocolError, RequestContext, unsupported_version)


def request(method, params=None, request_id=1):
    message = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params is not None:
        message['params'] = params
    return message


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.panel = FakePanel()
        basic_routes(self.panel)
        self.server = build_test_server()
        self.modern = RequestContext(ERA_MODERN, PROTOCOL_MODERN)
        self.legacy = RequestContext(ERA_LEGACY, '2025-06-18')

    def tearDown(self):
        self.panel.stop()

    # ------------------------------------------------------------- discovery

    def test_discover_reports_versions_capabilities_and_identity(self):
        result = self.server.dispatch(request('server/discover'), self.modern)['result']
        self.assertEqual(result['supportedVersions'], list(SUPPORTED_PROTOCOLS))
        self.assertIn('tools', result['capabilities'])
        self.assertEqual(result['_meta']['io.modelcontextprotocol/serverInfo']['name'],
                         'aapanel-mcp')
        self.assertTrue(result['instructions'])

    def test_discover_carries_cache_hints(self):
        result = self.server.dispatch(request('server/discover'), self.modern)['result']
        self.assertIsInstance(result['ttlMs'], int)
        self.assertGreaterEqual(result['ttlMs'], 0)
        self.assertEqual(result['cacheScope'], 'private')

    def test_modern_results_carry_result_type(self):
        result = self.server.dispatch(request('tools/list'), self.modern)['result']
        self.assertEqual(result['resultType'], 'complete')

    def test_legacy_results_omit_result_type(self):
        result = self.server.dispatch(request('tools/list'), self.legacy)['result']
        self.assertNotIn('resultType', result)

    # ------------------------------------------------------------- handshake

    def test_initialize_echoes_a_supported_version(self):
        message = request('initialize', {'protocolVersion': '2025-06-18',
                                         'clientInfo': {'name': 'test', 'version': '1'}})
        result = self.server.dispatch(message, self.legacy)['result']
        self.assertEqual(result['protocolVersion'], '2025-06-18')
        self.assertEqual(result['serverInfo']['name'], 'aapanel-mcp')

    def test_initialize_with_an_unknown_older_version_still_connects(self):
        message = request('initialize', {'protocolVersion': '2024-11-05'})
        result = self.server.dispatch(message, self.legacy)['result']
        self.assertIn(result['protocolVersion'], SUPPORTED_PROTOCOLS)

    def test_initialize_with_a_future_version_is_rejected(self):
        message = request('initialize', {'protocolVersion': '2099-01-01'})
        with self.assertRaises(ProtocolError) as caught:
            self.server.dispatch(message, self.legacy)
        self.assertEqual(caught.exception.code, CODE_UNSUPPORTED_VERSION)

    def test_unsupported_version_error_lists_what_is_supported(self):
        error = unsupported_version('1900-01-01')
        self.assertEqual(error.code, CODE_UNSUPPORTED_VERSION)
        self.assertEqual(error.data['requested'], '1900-01-01')
        self.assertIn(PROTOCOL_MODERN, error.data['supported'])

    # ----------------------------------------------------------------- basics

    def test_notifications_get_no_response(self):
        self.assertIsNone(self.server.dispatch(
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, self.modern))

    def test_ping(self):
        self.assertEqual(self.server.dispatch(request('ping'), self.modern)['result'],
                         {'resultType': 'complete'})

    def test_unknown_method_is_a_404_shaped_error(self):
        with self.assertRaises(ProtocolError) as caught:
            self.server.dispatch(request('resources/read', {'uri': 'x'}), self.modern)
        self.assertEqual(caught.exception.code, CODE_METHOD_NOT_FOUND)
        self.assertEqual(caught.exception.http_status, 404)

    def test_non_jsonrpc_message_is_rejected(self):
        with self.assertRaises(ProtocolError):
            self.server.dispatch({'id': 1, 'method': 'ping'}, self.modern)

    # ------------------------------------------------------------ tools/list

    def test_tools_list_shows_only_enabled_tiers(self):
        result = self.server.dispatch(request('tools/list'), self.modern)['result']
        names = {tool['name'] for tool in result['tools']}
        self.assertIn('site_list', names)          # read tier, on by default
        self.assertNotIn('site_create', names)     # write tier, off by default
        self.assertNotIn('site_delete', names)     # destructive tier, off by default
        self.assertNotIn('run_shell', names)       # shell tier, off by default

    def test_tools_list_carries_cache_hints(self):
        # 2026-07-28 requires both on every listing result, and a client that validates
        # its schema drops the whole tool list when either is missing.
        for ctx in (self.modern, self.legacy):
            result = self.server.dispatch(request('tools/list'), ctx)['result']
            self.assertIsInstance(result['ttlMs'], int)
            self.assertGreaterEqual(result['ttlMs'], 0)
            self.assertEqual(result['cacheScope'], 'private')

    def test_every_listed_tool_has_a_usable_schema(self):
        result = self.server.dispatch(request('tools/list'), self.modern)['result']
        for tool in result['tools']:
            self.assertTrue(tool['description'], tool['name'])
            self.assertEqual(tool['inputSchema']['type'], 'object', tool['name'])
            self.assertIn('annotations', tool)

    def test_destructive_tools_advertise_the_confirm_parameter(self):
        server = build_test_server({'destructive': True})
        result = server.dispatch(request('tools/list'), self.modern)['result']
        by_name = {tool['name']: tool for tool in result['tools']}
        self.assertIn('confirm', by_name['site_delete']['inputSchema']['properties'])
        self.assertNotIn('confirm', by_name['site_list']['inputSchema'].get('properties', {}))

    # ------------------------------------------------------------ tools/call

    def test_tool_call_returns_content_and_structured_content(self):
        result = self.server.dispatch(
            request('tools/call', {'name': 'site_list', 'arguments': {}}), self.modern)['result']
        self.assertFalse(result['isError'])
        self.assertEqual(result['content'][0]['type'], 'text')
        self.assertEqual(result['structuredContent']['count'], 2)

    def test_unknown_tool_is_a_protocol_error(self):
        with self.assertRaises(ProtocolError) as caught:
            self.server.dispatch(request('tools/call', {'name': 'nope'}), self.modern)
        self.assertEqual(caught.exception.code, CODE_INVALID_PARAMS)

    def test_bad_arguments_come_back_as_a_tool_error_not_a_protocol_error(self):
        # The model can fix these, so they belong in the result where it will see them.
        result = self.server.dispatch(
            request('tools/call', {'name': 'site_info', 'arguments': {}}), self.modern)['result']
        self.assertTrue(result['isError'])
        self.assertIn('site is required', result['content'][0]['text'])

    def test_unknown_argument_is_named_in_the_error(self):
        result = self.server.dispatch(
            request('tools/call', {'name': 'site_list', 'arguments': {'bogus': 1}}),
            self.modern)['result']
        self.assertTrue(result['isError'])
        self.assertIn('bogus', result['content'][0]['text'])

    def test_disabled_tool_call_explains_which_tier_to_enable(self):
        result = self.server.dispatch(
            request('tools/call', {'name': 'site_create',
                                   'arguments': {'domain': 'x.com'}}), self.modern)['result']
        self.assertTrue(result['isError'])
        self.assertIn('write', result['content'][0]['text'])

    def test_panel_failure_is_reported_with_the_remediation(self):
        self.panel.close_api()
        result = self.server.dispatch(
            request('tools/call', {'name': 'site_list', 'arguments': {}}), self.modern)['result']
        self.assertTrue(result['isError'])
        self.assertIn('What to do:', result['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
