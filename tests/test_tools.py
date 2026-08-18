# coding: utf-8
"""Tools: that they send the panel what it actually expects, and read it back sanely."""

import json
import unittest

from support import FakePanel, basic_routes, build_test_server

from mcpd.panel_client import PanelClient
from mcpd.registry import ToolContext, ToolError, validate
from mcpd.tools import build_registry
from mcpd.tools.common import resolve_site
from mcpd.tools.mail import classify, discover_methods

MAIL_SOURCE = '''
class mail_sys_main:
    def get_domains(self, get):
        return []

    def add_domain(self, get):
        domain = get.domain
        quota = get['quota']
        return True

    def delete_mailbox(self, get):
        return get.username

    def _private(self, get):
        return None
'''


class ToolCallTest(unittest.TestCase):
    """Calls tools directly, then asserts on what the fake panel received."""

    def setUp(self):
        self.panel_fake = FakePanel()
        basic_routes(self.panel_fake)
        self.registry = build_registry()
        self.client = PanelClient(timeout=5)
        self.ctx = ToolContext(self.client, {}, self.registry)

    def tearDown(self):
        self.panel_fake.stop()

    def call(self, name, arguments=None):
        tool = self.registry.get(name)
        self.assertIsNotNone(tool, 'no such tool: %s' % name)
        return tool.handler(self.ctx, validate(tool.input_schema, arguments or {}))

    def last(self):
        return self.panel_fake.calls[-1]['params']

    # ------------------------------------------------------------ resolution

    def test_site_resolves_by_name_id_and_domain(self):
        self.assertEqual(resolve_site(self.client, 'example.com')['id'], 1)
        self.assertEqual(resolve_site(self.client, '2')['name'], 'demo.test')
        self.assertEqual(resolve_site(self.client, 'EXAMPLE.COM')['id'], 1)

    def test_unknown_site_points_at_the_listing_tool(self):
        with self.assertRaises(ToolError) as caught:
            resolve_site(self.client, 'nope.invalid')
        self.assertIn('site_list', str(caught.exception))

    # ------------------------------------------- aaPanel methods that cannot be called
    # aaPanel dispatches every action as `method(get)`. Four of the methods these tools
    # used are either declared `def X(self)` (TypeError before the body runs) or listed
    # in a route whitelist without existing on the class (AttributeError). Either way the
    # panel's catch-all error handler answers 404, so a regression here looks like "this
    # panel version lacks the feature" rather than a bug. Pin the working data sources.

    def test_firewall_list_reads_the_table_not_the_uncallable_getlist(self):
        self.call('firewall_list')
        params = self.last()
        self.assertEqual(params['action'], 'getData')
        self.assertEqual(params['table'], 'firewall')

    def test_panel_logs_read_the_table_not_getopelogs(self):
        # GetOpeLogs reads a log file named by a `path` param; without one it raises.
        self.call('system_panel_logs')
        params = self.last()
        self.assertEqual(params['action'], 'getData')
        self.assertEqual(params['table'], 'logs')

    def test_system_version_uses_getsystemtotal(self):
        result = self.call('system_version')
        actions = [c['params'].get('action') for c in self.panel_fake.calls]
        self.assertIn('GetSystemTotal', actions)
        self.assertNotIn('GetSystemVersion', actions)
        self.assertIn('panel_version', result)

    def test_database_info_comes_from_the_table_not_getdatainfo(self):
        result = self.call('database_info', {'database': 'appdb'})
        actions = [c['params'].get('action') for c in self.panel_fake.calls]
        self.assertNotIn('GetdataInfo', actions)
        self.assertNotIn('GetInfo', actions)
        self.assertEqual(result['database']['name'], 'appdb')

    # ----------------------------------------------------------------- sites

    def test_site_list_trims_the_rows(self):
        result = self.call('site_list')
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['sites'][0]['name'], 'example.com')
        self.assertEqual(result['pagination'], 'total 2')

    def test_site_create_sends_the_webname_json_the_panel_parses(self):
        self.panel_fake.route('/site', 'AddSite', {'siteStatus': True})
        self.call('site_create', {'domain': 'New.Example.com',
                                  'extra_domains': ['www.new.example.com'],
                                  'php_version': '82'})
        params = self.last()
        webname = json.loads(params['webname'])
        self.assertEqual(webname['domain'], 'new.example.com')
        self.assertEqual(webname['domainlist'], ['www.new.example.com'])
        self.assertEqual(params['path'], '/www/wwwroot/new.example.com')
        self.assertEqual(params['version'], '82')

    def test_site_create_with_database_returns_the_generated_password(self):
        self.panel_fake.route('/site', 'AddSite', {'siteStatus': True})
        result = self.call('site_create', {'domain': 'shop.example', 'create_database': True})
        self.assertIn('database_password', result)
        self.assertEqual(self.last()['sql'], 'MySQL')
        self.assertEqual(self.last()['datapassword'], result['database_password'])

    def test_site_delete_maps_flags_to_the_panels_string_ones(self):
        self.panel_fake.route('/site', 'DeleteSite', {'status': True, 'msg': 'ok'})
        self.call('site_delete', {'site': 'example.com', 'delete_files': True,
                                  'delete_database': True})
        params = self.last()
        self.assertEqual(params['id'], '1')
        self.assertEqual(params['webname'], 'example.com')
        self.assertEqual(params['path'], '1')
        self.assertEqual(params['database'], '1')
        self.assertNotIn('ftp', params)

    def test_site_run_path_root_is_sent_as_empty(self):
        self.panel_fake.route('/site', 'SetSiteRunPath', {'status': True})
        self.call('site_set_run_path', {'site': 'example.com', 'run_path': '/'})
        self.assertEqual(self.last().get('runPath', ''), '')

    def test_panel_refusal_becomes_a_tool_error_with_the_panels_reason(self):
        self.panel_fake.route('/site', 'AddSite', {'status': False, 'msg': 'domain in use'})
        with self.assertRaises(ToolError) as caught:
            self.call('site_create', {'domain': 'taken.example'})
        self.assertIn('domain in use', str(caught.exception))

    # ------------------------------------------------------------------- ssl

    def test_issue_letsencrypt_defaults_to_the_bound_domains(self):
        self.panel_fake.route('/site', 'GetSiteDomains',
                              {'domains': [{'name': 'example.com'}, {'name': 'www.example.com'}]})
        self.panel_fake.route('/site', 'CreateLet', {'status': True, 'msg': 'issued'})
        self.call('ssl_issue_letsencrypt', {'site': 'example.com'})
        self.assertEqual(json.loads(self.last()['domains']),
                         ['example.com', 'www.example.com'])
        self.assertNotIn('dnsapi', self.last())

    def test_dns_validation_is_requested_when_an_api_is_named(self):
        self.panel_fake.route('/site', 'GetSiteDomains', {'domains': [{'name': 'example.com'}]})
        self.panel_fake.route('/site', 'CreateLet', {'status': True})
        self.call('ssl_issue_letsencrypt', {'site': 'example.com', 'dns_api': 'dns_cf'})
        self.assertEqual(self.last()['dnsapi'], 'dns_cf')

    # -------------------------------------------------------------- database

    def test_database_create_generates_a_password_and_echoes_it_once(self):
        self.panel_fake.route('/database', 'AddDatabase', {'status': True})
        result = self.call('database_create', {'name': 'shop'})
        self.assertEqual(self.last()['db_user'], 'shop')
        self.assertEqual(self.last()['codeing'], 'utf8mb4')
        self.assertEqual(self.last()['password'], result['password'])
        self.assertGreaterEqual(len(result['password']), 16)

    # ------------------------------------------------------------------ cron

    def test_cron_create_sends_every_key_the_panel_reads_unconditionally(self):
        self.panel_fake.route('/crontab', 'AddCrontab', {'status': True, 'id': 3})
        self.call('cron_create', {'name': 'nightly', 'kind': 'shell',
                                  'script': 'echo hi', 'schedule': 'day', 'hour': 3})
        params = self.last()
        for key in ('name', 'type', 'where1', 'hour', 'minute', 'save', 'backupTo',
                    'sType', 'sName', 'sBody', 'urladdress'):
            self.assertIn(key, params, key)
        self.assertEqual(params['sType'], 'toShell')
        self.assertEqual(params['sBody'], 'echo hi')

    def test_cron_interval_schedules_use_where1(self):
        self.panel_fake.route('/crontab', 'AddCrontab', {'status': True})
        self.call('cron_create', {'name': 'often', 'kind': 'url',
                                  'url': 'https://example.com/ping',
                                  'schedule': 'minute-n', 'interval': 15})
        self.assertEqual(self.last()['where1'], '15')
        self.assertEqual(self.last()['sType'], 'toUrl')

    def test_cron_kind_without_its_required_field_is_refused_before_the_call(self):
        with self.assertRaises(ToolError):
            self.call('cron_create', {'name': 'broken', 'kind': 'shell'})
        self.assertEqual(self.panel_fake.calls, [])

    # ----------------------------------------------------------------- shell

    def test_run_shell_starts_the_command_then_polls_for_output(self):
        state = {'polls': 0}

        def poll(params):
            state['polls'] += 1
            if state['polls'] < 2:
                return {'status': False, 'msg': 'working'}
            return {'status': True, 'msg': 'hello\n'}

        self.panel_fake.route('/files', 'ExecShell', {'status': True, 'msg': 'Command sent'})
        self.panel_fake.route('/files', 'GetExecShellMsg', poll)
        result = self.call('run_shell', {'command': 'echo hello', 'timeout': 10})
        self.assertTrue(result['finished'])
        self.assertEqual(result['output'], 'hello\n')
        self.assertGreaterEqual(state['polls'], 2)

    def test_run_shell_that_outlives_its_timeout_says_where_to_look(self):
        self.panel_fake.route('/files', 'ExecShell', {'status': True})
        self.panel_fake.route('/files', 'GetExecShellMsg', {'status': False, 'msg': 'partial'})
        result = self.call('run_shell', {'command': 'sleep 99', 'timeout': 1})
        self.assertFalse(result['finished'])
        self.assertIn('shell_last_output', result['note'])

    # ----------------------------------------------------------------- files

    def test_file_list_splits_directories_from_files(self):
        self.panel_fake.route('/files', 'GetDir',
                              {'PATH': '/www', 'DIR': ['wwwroot;4096;...'],
                               'FILES': ['a.txt;10;...'], 'count': 2})
        result = self.call('file_list', {'path': '/www'})
        self.assertEqual(result['directories'], ['wwwroot;4096;...'])
        self.assertEqual(result['files'], ['a.txt;10;...'])

    def test_file_delete_picks_the_directory_endpoint(self):
        self.panel_fake.route('/files', 'DeleteDir', {'status': True})
        self.call('file_delete', {'path': '/tmp/x', 'is_directory': True})
        self.assertEqual(self.last()['action'], 'DeleteDir')

    # ------------------------------------------------------------- firewall

    def test_blocking_an_address_uses_the_panels_port_parameter(self):
        self.panel_fake.route('/firewall', 'AddDropAddress', {'status': True})
        self.call('firewall_block_ip', {'address': '203.0.113.9'})
        self.assertEqual(self.last()['port'], '203.0.113.9')

    # ------------------------------------------------------------------ raw

    def test_raw_call_cannot_forge_its_own_credentials(self):
        self.panel_fake.route('/system', 'GetLoadAverage', {'one': 0.5})
        self.call('panel_raw_call', {'route': '/system', 'action': 'GetLoadAverage',
                                     'params': {'request_token': 'forged',
                                                'request_time': '1'}})
        self.assertNotEqual(self.last()['request_token'], 'forged')

    def test_raw_call_requires_an_absolute_route(self):
        with self.assertRaises(ToolError):
            self.call('panel_raw_call', {'route': 'site'})

    def test_plugin_call_refuses_a_plugin_that_is_not_installed(self):
        with self.assertRaises(ToolError) as caught:
            self.call('panel_plugin_call', {'plugin': 'ghost', 'method': 'x'})
        self.assertIn('app_list', str(caught.exception))


class MailDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.panel_fake = FakePanel()
        self.panel_fake.install_plugin('mail_sys', MAIL_SOURCE)
        self.registry = build_registry()
        self.ctx = ToolContext(PanelClient(timeout=5), {}, self.registry)

    def tearDown(self):
        self.panel_fake.stop()

    def test_methods_and_their_parameters_are_read_from_the_plugin_source(self):
        methods = discover_methods('mail_sys')
        self.assertIn('get_domains', methods)
        self.assertEqual(methods['add_domain'], ['domain', 'quota'])
        self.assertNotIn('_private', methods)

    def test_methods_are_classified_by_verb(self):
        self.assertEqual(classify('get_domains'), 'read')
        self.assertEqual(classify('add_domain'), 'write')
        self.assertEqual(classify('delete_mailbox'), 'destructive')

    def test_capabilities_groups_them_for_the_agent(self):
        tool = self.registry.get('mail_capabilities')
        result = tool.handler(self.ctx, {'filter': ''})
        self.assertEqual(result['plugin'], 'mail_sys')
        self.assertIn('get_domains', result['reads'])
        self.assertIn('add_domain', result['changes'])
        self.assertIn('delete_mailbox', result['deletes'])

    def test_a_destructive_method_cannot_be_smuggled_through_mail_read(self):
        tool = self.registry.get('mail_read')
        with self.assertRaises(ToolError) as caught:
            tool.handler(self.ctx, {'method': 'delete_mailbox', 'params': {}})
        self.assertIn('mail_delete', str(caught.exception))

    def test_an_unknown_method_is_refused(self):
        tool = self.registry.get('mail_read')
        with self.assertRaises(ToolError) as caught:
            tool.handler(self.ctx, {'method': 'get_nonsense', 'params': {}})
        self.assertIn('mail_capabilities', str(caught.exception))

    def test_mail_tools_disappear_when_no_mail_plugin_is_installed(self):
        panel = self.ctx.panel
        self.assertTrue(self.registry.get('mail_status').is_available(panel))
        import shutil
        import os
        from mcpd.config import panel_home
        shutil.rmtree(os.path.join(panel_home(), 'plugin', 'mail_sys'))
        self.assertFalse(self.registry.get('mail_status').is_available(panel))


if __name__ == '__main__':
    unittest.main()
