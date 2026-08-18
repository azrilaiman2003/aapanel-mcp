# coding: utf-8
"""The Streamable HTTP transport, exercised over a real socket."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from support import FakePanel, basic_routes, build_test_server

from mcpd import PROTOCOL_MODERN
from mcpd import config as cfg
from mcpd.http_transport import HttpApp, decode_header_value


class HttpTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = FakePanel()
        basic_routes(cls.panel)

        config = cfg.load()
        config['bind'].update({'host': '127.0.0.1', 'port': 0, 'path': '/mcp'})
        config['auth']['token'] = 'test-token'
        config['auth']['origin_allowlist'] = ['https://allowed.example']
        cfg.save(config)

        cls.server = build_test_server()
        cls.app = HttpApp(lambda: cfg.load(), cls.server)
        # Bind on an ephemeral port, then publish the one the OS handed us.
        cls.thread = threading.Thread(target=cls.app.serve_forever, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.app._httpd is not None:
                break
            threading.Event().wait(0.05)
        cls.port = cls.app._httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.panel.stop()

    # ------------------------------------------------------------- helpers

    def post(self, body, headers=None, token='test-token', raw=None):
        data = raw if raw is not None else json.dumps(body).encode('utf-8')
        request = urllib.request.Request('http://127.0.0.1:%d/mcp' % self.port,
                                         data=data, method='POST')
        request.add_header('Content-Type', 'application/json')
        request.add_header('Accept', 'application/json, text/event-stream')
        if token:
            request.add_header('Authorization', 'Bearer %s' % token)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.getcode(), response.read().decode('utf-8')
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode('utf-8')

    def modern(self, method, params=None, request_id=1, extra_headers=None, name=None):
        params = dict(params or {})
        params.setdefault('_meta', {})['io.modelcontextprotocol/protocolVersion'] = PROTOCOL_MODERN
        headers = {'MCP-Protocol-Version': PROTOCOL_MODERN, 'Mcp-Method': method}
        if name:
            headers['Mcp-Name'] = name
        headers.update(extra_headers or {})
        body = {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}
        return self.post(body, headers)

    # ---------------------------------------------------------------- gates

    def test_missing_token_is_401(self):
        status, _ = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, token=None)
        self.assertEqual(status, 401)

    def test_wrong_token_is_401(self):
        status, _ = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, token='nope')
        self.assertEqual(status, 401)

    def test_unlisted_origin_is_403(self):
        status, _ = self.modern('ping', extra_headers={'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)

    def test_allowlisted_origin_passes(self):
        status, _ = self.modern('ping', extra_headers={'Origin': 'https://allowed.example'})
        self.assertEqual(status, 200)

    def test_wrong_path_is_404(self):
        request = urllib.request.Request('http://127.0.0.1:%d/other' % self.port,
                                         data=b'{}', method='POST')
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                status = response.getcode()
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 404)

    def test_health_endpoint_needs_no_token(self):
        with urllib.request.urlopen('http://127.0.0.1:%d/healthz' % self.port,
                                    timeout=5) as response:
            payload = json.loads(response.read().decode('utf-8'))
        self.assertEqual(payload['status'], 'ok')

    # ----------------------------------------------------- header validation

    def test_protocol_version_header_is_required_for_modern_requests(self):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping',
                'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': PROTOCOL_MODERN}}}
        status, text = self.post(body, {'Mcp-Method': 'ping'})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(text)['error']['code'], -32020)

    def test_method_header_must_match_the_body(self):
        status, text = self.modern('tools/list', extra_headers={'Mcp-Method': 'tools/call'})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(text)['error']['code'], -32020)

    def test_name_header_must_match_the_body(self):
        status, text = self.modern('tools/call', {'name': 'site_list', 'arguments': {}},
                                   name='something_else')
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(text)['error']['code'], -32020)

    def test_name_header_is_required_for_tool_calls(self):
        status, text = self.modern('tools/call', {'name': 'site_list', 'arguments': {}})
        self.assertEqual(status, 400)
        self.assertIn('Mcp-Name', json.loads(text)['error']['message'])

    def test_base64_encoded_name_header_is_decoded_before_comparing(self):
        self.assertEqual(decode_header_value('=?base64?c2l0ZV9saXN0?='), 'site_list')
        status, _ = self.modern('tools/call', {'name': 'site_list', 'arguments': {}},
                                name='=?base64?c2l0ZV9saXN0?=')
        self.assertEqual(status, 200)

    def test_unsupported_protocol_version_lists_the_supported_ones(self):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping',
                'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': '1999-01-01'}}}
        status, text = self.post(body, {'MCP-Protocol-Version': '1999-01-01',
                                        'Mcp-Method': 'ping'})
        self.assertEqual(status, 400)
        error = json.loads(text)['error']
        self.assertEqual(error['code'], -32022)
        self.assertIn(PROTOCOL_MODERN, error['data']['supported'])

    # ------------------------------------------------------------- messages

    def test_notification_is_accepted_with_202_and_no_body(self):
        status, text = self.post({'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                                 {'MCP-Protocol-Version': PROTOCOL_MODERN,
                                  'Mcp-Method': 'notifications/initialized'})
        self.assertEqual(status, 202)
        self.assertEqual(text, '')

    def test_unknown_method_is_404_with_a_jsonrpc_error(self):
        status, text = self.modern('does/not/exist')
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(text)['error']['code'], -32601)

    def test_invalid_json_is_a_parse_error(self):
        status, text = self.post(None, {'MCP-Protocol-Version': PROTOCOL_MODERN},
                                 raw=b'{not json')
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(text)['error']['code'], -32700)

    def test_batched_messages_are_refused_by_name(self):
        status, text = self.post(None, {'MCP-Protocol-Version': PROTOCOL_MODERN},
                                 raw=b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]')
        self.assertEqual(status, 400)
        self.assertIn('array', json.loads(text)['error']['message'])

    def test_discover_over_http(self):
        status, text = self.modern('server/discover')
        self.assertEqual(status, 200)
        result = json.loads(text)['result']
        self.assertEqual(result['resultType'], 'complete')
        self.assertIn(PROTOCOL_MODERN, result['supportedVersions'])

    def test_tool_call_over_http(self):
        status, text = self.modern('tools/call', {'name': 'site_list', 'arguments': {}},
                                   name='site_list')
        self.assertEqual(status, 200)
        result = json.loads(text)['result']
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['count'], 2)

    # --------------------------------------------------------------- legacy

    def test_legacy_client_without_any_version_header_still_works(self):
        # Pre-2025-06-18 clients send no version header at all; the transport spec lets
        # a server read that as the oldest Streamable HTTP revision.
        status, text = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual(status, 200)
        self.assertNotIn('resultType', json.loads(text)['result'])

    def test_legacy_initialize_handshake(self):
        status, text = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                  'params': {'protocolVersion': '2025-06-18',
                                             'capabilities': {},
                                             'clientInfo': {'name': 'legacy', 'version': '1'}}})
        self.assertEqual(status, 200)
        result = json.loads(text)['result']
        self.assertEqual(result['protocolVersion'], '2025-06-18')
        self.assertNotIn('resultType', result)

    # --------------------------------------------------------- subscriptions

    def test_listen_opens_a_stream_and_acknowledges_what_it_will_send(self):
        import http.client
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        body = json.dumps({
            'jsonrpc': '2.0', 'id': 7, 'method': 'subscriptions/listen',
            'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': PROTOCOL_MODERN},
                       'notifications': {'toolsListChanged': True,
                                         'resourcesListChanged': True}}})
        connection.request('POST', '/mcp', body, {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer test-token',
            'MCP-Protocol-Version': PROTOCOL_MODERN,
            'Mcp-Method': 'subscriptions/listen'})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader('Content-Type').startswith('text/event-stream'))
        self.assertEqual(response.getheader('X-Accel-Buffering'), 'no')

        event = json.loads(response.readline().decode('utf-8').split('data: ', 1)[1])
        connection.close()

        self.assertEqual(event['method'], 'notifications/subscriptions/acknowledged')
        self.assertEqual(event['params']['_meta']['io.modelcontextprotocol/subscriptionId'], 7)
        # Only what the server actually honours comes back in the acknowledgment.
        self.assertEqual(event['params']['notifications'], {'toolsListChanged': True})

    def test_the_tool_signature_moves_when_permissions_change(self):
        # This is what the listen stream watches to decide it should notify.
        before = self.server.tools_signature()
        config = cfg.load()
        config['permissions']['tiers']['write'] = True
        cfg.save(config)
        try:
            self.assertNotEqual(self.server.tools_signature(), before)
        finally:
            config['permissions']['tiers']['write'] = False
            cfg.save(config)

    # -------------------------------------------------------- startup failures
    # A daemon that dies on startup and a daemon that was never enabled look identical
    # from a client ("connection refused"), so the log has to say which one happened.

    def _failing_app(self, mutate):
        config = cfg.load()
        mutate(config)
        lines = []
        app = HttpApp(lambda: config, self.server, lambda kind, msg: lines.append((kind, msg)))
        with self.assertRaises(Exception):
            app.serve_forever()
        return lines

    def test_an_address_not_on_this_machine_says_so(self):
        # 192.0.2.1 is TEST-NET-1: reserved, never assigned to a real interface.
        lines = self._failing_app(lambda c: c['bind'].update({'host': '192.0.2.1', 'port': 47999}))
        errors = [msg for kind, msg in lines if kind == 'error']
        self.assertTrue(errors, 'the failure should have been logged')
        self.assertIn('cannot bind', errors[0])
        self.assertIn('0.0.0.0', errors[0], 'the log should name the fix')

    def test_a_missing_certificate_is_reported_before_the_port_is_claimed(self):
        def mutate(config):
            config['bind'].update({'host': '127.0.0.1', 'port': 47998})
            config['bind']['tls'] = {'mode': 'custom', 'cert': '/no/such.pem', 'key': '/no/such.key'}

        lines = self._failing_app(mutate)
        self.assertIn('not starting', [msg for kind, msg in lines if kind == 'error'][0])
        # Nothing may be listening on that port: TLS is settled before the bind.
        import socket
        probe = socket.socket()
        probe.settimeout(2)
        try:
            self.assertRaises(OSError, probe.connect, ('127.0.0.1', 47998))
        finally:
            probe.close()

    def test_legacy_session_delete_is_accepted(self):
        request = urllib.request.Request('http://127.0.0.1:%d/mcp' % self.port, method='DELETE')
        request.add_header('Authorization', 'Bearer test-token')
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.getcode(), 200)


if __name__ == '__main__':
    unittest.main()
