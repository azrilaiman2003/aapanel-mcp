# coding: utf-8
"""The stdio transport: framing, era selection and error handling."""

import io
import json
import unittest

from support import FakePanel, basic_routes, build_test_server

from mcpd import PROTOCOL_MODERN
from mcpd.stdio_transport import StdioTransport


class StdioTest(unittest.TestCase):
    def setUp(self):
        self.panel = FakePanel()
        basic_routes(self.panel)
        self.server = build_test_server()

    def tearDown(self):
        self.panel.stop()

    def run_lines(self, *messages):
        stdin = io.StringIO(''.join(json.dumps(m) + '\n' for m in messages))
        stdout, stderr = io.StringIO(), io.StringIO()
        StdioTransport(self.server, stdin, stdout, stderr).run()
        return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]

    @staticmethod
    def modern(method, params=None, request_id=1):
        params = dict(params or {})
        params.setdefault('_meta', {})['io.modelcontextprotocol/protocolVersion'] = PROTOCOL_MODERN
        return {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}

    def test_one_response_per_request_line(self):
        responses = self.run_lines(self.modern('ping', request_id=1),
                                   self.modern('tools/list', request_id=2))
        self.assertEqual([r['id'] for r in responses], [1, 2])

    def test_modern_probe_gets_a_discover_result(self):
        responses = self.run_lines(self.modern('server/discover'))
        self.assertIn(PROTOCOL_MODERN, responses[0]['result']['supportedVersions'])
        self.assertEqual(responses[0]['result']['resultType'], 'complete')

    def test_initialize_switches_the_process_to_legacy_shapes(self):
        responses = self.run_lines(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-11-25', 'clientInfo': {'name': 'c'}}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        self.assertEqual(responses[0]['result']['protocolVersion'], '2025-11-25')
        # Sticky: the follow-up has no _meta of its own and must stay legacy.
        self.assertNotIn('resultType', responses[1]['result'])

    def test_notifications_produce_no_output(self):
        self.assertEqual(self.run_lines({'jsonrpc': '2.0',
                                         'method': 'notifications/initialized'}), [])

    def test_blank_lines_are_ignored(self):
        stdin = io.StringIO('\n\n' + json.dumps(self.modern('ping')) + '\n')
        stdout = io.StringIO()
        StdioTransport(self.server, stdin, stdout, io.StringIO()).run()
        self.assertEqual(len(stdout.getvalue().strip().splitlines()), 1)

    def test_broken_json_gets_a_parse_error_not_a_crash(self):
        stdin = io.StringIO('{oops\n')
        stdout = io.StringIO()
        StdioTransport(self.server, stdin, stdout, io.StringIO()).run()
        self.assertEqual(json.loads(stdout.getvalue())['error']['code'], -32700)

    def test_unsupported_version_is_reported(self):
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list',
                   'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': '1999-01-01'}}}
        responses = self.run_lines(message)
        self.assertEqual(responses[0]['error']['code'], -32022)

    def test_tool_call_over_stdio(self):
        responses = self.run_lines(
            self.modern('tools/call', {'name': 'site_list', 'arguments': {}}))
        self.assertEqual(responses[0]['result']['structuredContent']['count'], 2)


if __name__ == '__main__':
    unittest.main()
