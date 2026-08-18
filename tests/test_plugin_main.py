# coding: utf-8
"""The panel-side control surface that index.html calls."""

import json
import os
import unittest

from support import FakePanel, basic_routes

import aapanel_mcp_main
from mcpd import config as cfg


class Args(dict):
    """Stands in for aaPanel's dict_obj, which allows both attribute and item access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class ControlSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.panel = FakePanel()
        basic_routes(self.panel)
        self.plugin = aapanel_mcp_main.aapanel_mcp_main()

    def tearDown(self):
        self.panel.stop()

    # --------------------------------------------------------------- status

    def test_status_reports_everything_the_overview_renders(self):
        status = self.plugin.get_status()
        self.assertTrue(status['status'])
        self.assertIn('running', status['service'])
        self.assertTrue(status['panel_api']['ok'])
        self.assertEqual(status['tool_counts']['total'], len(self.plugin.registry))
        self.assertGreater(status['tool_counts']['enabled'], 0)
        self.assertIn('sites', status['tool_counts']['by_domain'])
        self.assertTrue(status['endpoint'].endswith('/mcp'))

    def test_enabled_count_matches_the_read_only_default(self):
        status = self.plugin.get_status()
        counts = status['tool_counts']
        self.assertLess(counts['enabled'], counts['total'])
        self.assertTrue(status['tiers']['read'])
        self.assertFalse(status['tiers']['shell'])

    def test_panel_api_test_makes_a_real_call(self):
        result = self.plugin.test_panel_api()
        self.assertTrue(result['status'])
        self.assertEqual(result['sample']['one'], 0.1)

    # ------------------------------------------------------------ panel api

    def _api_config(self):
        with open(self.plugin.panel.api_config_path, encoding='utf-8') as fp:
            return json.load(fp)

    def test_enabling_the_panel_api_opens_it_for_loopback(self):
        os.remove(self.plugin.panel.api_config_path)
        result = self.plugin.enable_panel_api()
        self.assertTrue(result['status'], result['msg'])
        written = self._api_config()
        self.assertTrue(written['open'])
        self.assertIn('127.0.0.1', written['limit_addr'])
        self.assertTrue(written['token'])
        self.assertTrue(result['api_key'], 'a key should have been generated')

    def test_enabling_keeps_an_existing_key(self):
        before = self._api_config()['token']
        result = self.plugin.enable_panel_api()
        after = self._api_config()['token']
        self.assertEqual(before, after)
        self.assertEqual(result['api_key'], '')

    def test_the_api_config_is_not_world_readable(self):
        self.plugin.enable_panel_api()
        mode = os.stat(self.plugin.panel.api_config_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    # --------------------------------------------------------------- config

    def _save(self, payload):
        return self.plugin.save_config(Args(data=json.dumps(payload)))

    def test_saving_a_valid_config(self):
        result = self._save({'bind': {'mode': 'bound', 'host': '0.0.0.0', 'port': 9100,
                                      'path': '/mcp', 'tls': {'mode': 'self_signed'}},
                             'limits': {'rate_per_minute': 60}})
        self.assertTrue(result['status'], result['msg'])
        config = cfg.load()
        self.assertEqual(config['bind']['port'], 9100)
        self.assertEqual(config['bind']['host'], '0.0.0.0')
        self.assertEqual(config['limits']['rate_per_minute'], 60)

    def test_a_bad_port_is_refused_with_a_reason(self):
        result = self._save({'bind': {'port': 70000}})
        self.assertFalse(result['status'])
        self.assertIn('between 1 and 65535', result['msg'])

    def test_a_non_numeric_port_is_refused(self):
        self.assertFalse(self._save({'bind': {'port': 'eight thousand'}})['status'])

    def test_an_unknown_bind_mode_is_refused(self):
        self.assertFalse(self._save({'bind': {'mode': 'carrier-pigeon'}})['status'])

    def test_a_custom_certificate_must_exist(self):
        result = self._save({'bind': {'mode': 'bound',
                                      'tls': {'mode': 'custom', 'cert': '/no/such.pem',
                                              'key': '/no/such.key'}}})
        self.assertFalse(result['status'])
        self.assertIn('not found', result['msg'])

    def test_a_path_without_a_leading_slash_is_refused(self):
        self.assertFalse(self._save({'bind': {'path': 'mcp'}})['status'])

    def test_loopback_modes_pin_the_host_and_the_forwarded_header(self):
        self._save({'bind': {'mode': 'bound', 'host': '0.0.0.0'}})
        self._save({'bind': {'mode': 'localhost', 'host': '0.0.0.0'}})
        config = cfg.load()
        self.assertEqual(config['bind']['host'], '127.0.0.1')
        self.assertFalse(config['bind']['trust_forwarded'])

    def test_a_network_port_on_a_loopback_address_is_refused(self):
        # This combination starts cleanly, logs nothing unusual, and refuses every remote
        # client. It has to be caught at save time or it is invisible until someone tries
        # to connect from another machine.
        result = self._save({'bind': {'mode': 'bound', 'host': '127.0.0.1'}})
        self.assertFalse(result['status'])
        self.assertIn('0.0.0.0', result['msg'])
        self.assertEqual(cfg.load()['bind']['mode'], 'localhost', 'nothing should be saved')

    def test_the_loopback_check_holds_when_only_the_mode_is_sent(self):
        # A client that sends {"bind": {"mode": "bound"}} alone inherits the stored
        # 127.0.0.1, so the check cannot live in per-field validation.
        self.assertFalse(self._save({'bind': {'mode': 'bound'}})['status'])

    def test_ipv6_loopback_is_refused_too(self):
        self.assertFalse(self._save({'bind': {'mode': 'bound', 'host': '::1'}})['status'])

    def test_a_real_listen_address_is_accepted(self):
        self.assertTrue(self._save({'bind': {'mode': 'bound', 'host': '0.0.0.0'}})['status'])
        self.assertEqual(cfg.load()['bind']['host'], '0.0.0.0')

    def test_an_install_already_stuck_on_loopback_is_warned_about(self):
        config = cfg.load()
        config['bind'].update({'mode': 'bound', 'host': '127.0.0.1'})
        cfg.save(config)
        warnings = ' '.join(self.plugin.get_status()['warnings'])
        self.assertIn('connection refused', warnings)
        self.assertIn('0.0.0.0', warnings)

    def test_a_healthy_bind_warns_about_nothing(self):
        config = cfg.load()
        config['bind'].update({'mode': 'bound', 'host': '0.0.0.0'})
        config['bind']['tls'] = {'mode': 'self_signed', 'cert': '', 'key': ''}
        cfg.save(config)
        self.assertEqual(self.plugin.get_status()['warnings'], [])

    def test_a_network_port_without_tls_is_called_out(self):
        config = cfg.load()
        config['bind'].update({'mode': 'bound', 'host': '0.0.0.0'})
        cfg.save(config)
        self.assertIn('clear text', ' '.join(self.plugin.get_status()['warnings']))

    def test_proxy_mode_trusts_the_forwarded_header(self):
        self._save({'bind': {'mode': 'proxy'}})
        self.assertTrue(cfg.load()['bind']['trust_forwarded'])

    def test_an_empty_body_is_refused_rather_than_wiping_the_config(self):
        self.assertFalse(self.plugin.save_config(Args(data=''))['status'])
        self.assertFalse(self.plugin.save_config(Args(data='{not json'))['status'])
        self.assertTrue(cfg.load()['auth']['token'])

    def test_regenerating_the_token_changes_it(self):
        before = cfg.load()['auth']['token']
        result = self.plugin.regenerate_token()
        self.assertTrue(result['status'])
        self.assertNotEqual(result['token'], before)
        self.assertEqual(cfg.load()['auth']['token'], result['token'])

    # ---------------------------------------------------------- permissions

    def test_tiers_can_be_switched(self):
        self.assertTrue(self.plugin.set_tier(Args(tier='write', enabled='1'))['status'])
        self.assertTrue(cfg.load()['permissions']['tiers']['write'])
        self.plugin.set_tier(Args(tier='write', enabled='0'))
        self.assertFalse(cfg.load()['permissions']['tiers']['write'])

    def test_an_unknown_tier_is_refused(self):
        self.assertFalse(self.plugin.set_tier(Args(tier='wizard', enabled='1'))['status'])

    def test_a_single_tool_can_be_forced_on_or_off(self):
        self.plugin.set_tool(Args(tool='site_create', state='1'))
        self.assertTrue(cfg.load()['permissions']['tools']['site_create'])
        self.plugin.set_tool(Args(tool='site_create', state='default'))
        self.assertNotIn('site_create', cfg.load()['permissions']['tools'])

    def test_an_unknown_tool_is_refused(self):
        self.assertFalse(self.plugin.set_tool(Args(tool='make_coffee', state='1'))['status'])

    def test_presets_set_the_whole_posture(self):
        self.plugin.apply_preset(Args(preset='full'))
        tiers = cfg.load()['permissions']['tiers']
        self.assertTrue(all(tiers.values()))
        self.plugin.apply_preset(Args(preset='read_only'))
        tiers = cfg.load()['permissions']['tiers']
        self.assertTrue(tiers['read'])
        self.assertFalse(any(v for k, v in tiers.items() if k != 'read'))

    def test_presets_clear_per_tool_overrides(self):
        self.plugin.set_tool(Args(tool='run_shell', state='1'))
        self.plugin.apply_preset(Args(preset='read_only'))
        self.assertEqual(cfg.load()['permissions']['tools'], {})

    def test_the_tool_list_carries_what_the_permissions_table_shows(self):
        data = self.plugin.get_tools()
        row = next(tool for tool in data['tools'] if tool['name'] == 'site_list')
        for key in ('title', 'description', 'domain', 'tier', 'available', 'enabled'):
            self.assertIn(key, row)
        self.assertTrue(row['enabled'])

    # -------------------------------------------------------------- clients

    def test_client_snippets_carry_the_live_token_and_endpoint(self):
        snippets = self.plugin.get_client_config()
        token = cfg.load()['auth']['token']
        self.assertIn(token, snippets['claude_code'])
        self.assertIn(token, snippets['json'])
        self.assertIn(snippets['url'], snippets['claude_code'])
        self.assertIn('aapanel-mcp-stdio', snippets['claude_code_stdio'])
        self.assertEqual(json.loads(snippets['json'])['mcpServers']['aapanel']['type'], 'http')

    def test_audit_reads_back_through_the_control_surface(self):
        from mcpd.audit import AuditLog
        AuditLog(cfg.load()).record(tool='site_list', outcome='ok')
        self.assertEqual(self.plugin.get_audit(Args(limit=10))['entries'][0]['tool'], 'site_list')
        self.plugin.clear_audit()
        self.assertEqual(self.plugin.get_audit(Args(limit=10))['entries'], [])


if __name__ == '__main__':
    unittest.main()
