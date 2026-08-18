# coding: utf-8
"""The panel client: token derivation, route fallback, and readable failures."""

import unittest

from support import FakePanel, basic_routes, md5

from mcpd.panel_client import PanelApiError, PanelClient, panel_version


class PanelClientTest(unittest.TestCase):
    def setUp(self):
        self.panel = FakePanel()
        basic_routes(self.panel)
        self.client = PanelClient(timeout=5)

    def tearDown(self):
        self.panel.stop()

    def test_signs_requests_the_way_the_panel_verifies_them(self):
        self.client.call('/system', 'GetLoadAverage')
        call = self.panel.calls[-1]
        expected = md5(call['params']['request_time'] + md5(self.panel.api_sk))
        self.assertEqual(call['params']['request_token'], expected)

    def test_reads_the_key_from_api_json_without_being_told(self):
        # No secret is configured anywhere in the client; it comes off disk.
        self.assertEqual(self.client.call('/system', 'GetLoadAverage')['one'], 0.1)

    def test_status_reports_a_closed_api_with_a_fix(self):
        self.panel.close_api()
        state = self.client.status()
        self.assertFalse(state['ok'])
        self.assertIn('switched off', state['reason'])
        self.assertIn('Enable local panel API', state['remediation'])

    def test_closed_api_raises_with_remediation_attached(self):
        self.panel.close_api()
        with self.assertRaises(PanelApiError) as caught:
            self.client.call('/system', 'GetLoadAverage')
        self.assertIn('Enable local panel API', caught.exception.remediation)

    def test_missing_loopback_in_allowlist_is_reported(self):
        self.panel.limit_addr = ['10.0.0.5']
        self.panel.write_api_config()
        state = self.client.status()
        self.assertFalse(state['ok'])
        self.assertIn('127.0.0.1', state['reason'])

    def test_login_page_response_is_translated(self):
        # A wrong token makes the real panel answer with HTML.
        self.panel.api_sk = 'rotated-key'
        with self.assertRaises(PanelApiError) as caught:
            self.client.call('/system', 'GetLoadAverage')
        self.assertIn('login page', caught.exception.message)

    def test_falls_back_to_the_v2_route_prefix(self):
        self.panel.route('/v2/site', 'GetSiteDomains', {'domains': [{'name': 'example.com'}]})
        result = self.client.call('/site', 'GetSiteDomains', id=1)
        self.assertEqual(result['domains'][0]['name'], 'example.com')
        self.assertEqual(self.panel.calls[-1]['path'], '/v2/site')

    def test_remembers_which_prefix_worked(self):
        self.panel.route('/v2/site', 'GetSSL', {'status': True})
        self.client.call('/site', 'GetSSL', siteName='a')
        before = len(self.panel.calls)
        self.client.call('/site', 'GetSSL', siteName='b')
        # Second call goes straight to /v2 instead of probing the bare route again.
        self.assertEqual(len(self.panel.calls), before + 1)

    def test_a_404_names_the_action_and_does_not_guess_at_the_cause(self):
        # aaPanel serves its not-logged-in page — as a 404 — whenever a panel method
        # raises, and token-authenticated calls never have a session. So a 404 does not
        # distinguish "no such route" from "that handler threw", and claiming the former
        # sends whoever is reading straight past the traceback that explains it.
        with self.assertRaises(PanelApiError) as caught:
            self.client.call('/nope', 'Whatever')
        error = caught.exception
        self.assertIn('/nope?action=Whatever', error.message)
        self.assertNotIn('no endpoint', error.message)
        self.assertIn('error.log', error.remediation)
        self.assertIn('exception', error.remediation)

    def test_a_404_hands_back_what_the_panel_actually_said(self):
        with self.assertRaises(PanelApiError) as caught:
            self.client.call('/nope', 'Whatever')
        self.assertIn('panel_response', caught.exception.to_dict())

    def test_structured_values_are_json_encoded(self):
        self.panel.route('/site', 'CreateLet', {'status': True})
        self.client.call('/site', 'CreateLet', domains=['a.com', 'b.com'], flag=True)
        params = self.panel.calls[-1]['params']
        self.assertEqual(params['domains'], '["a.com","b.com"]')
        self.assertEqual(params['flag'], 'true')

    def test_panel_version_is_read_from_source(self):
        self.assertEqual(panel_version(), '8.21.0')

    def test_plugin_presence_check(self):
        self.panel.install_plugin('mail_sys')
        self.assertTrue(self.client.plugin_installed('mail_sys'))
        self.assertFalse(self.client.plugin_installed('not_installed'))


if __name__ == '__main__':
    unittest.main()
