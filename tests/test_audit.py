# coding: utf-8
"""The audit log: what it records, and what it must never record."""

import json
import os
import unittest

from support import FakePanel, basic_routes, build_test_server

from mcpd import config as cfg
from mcpd.audit import AuditLog, redact
from mcpd.protocol import ERA_MODERN, RequestContext


class RedactionTest(unittest.TestCase):
    def test_fields_that_look_like_secrets_are_masked(self):
        cleaned = redact({'user': 'bob', 'password': 'hunter2', 'ftp_password': 'x',
                          'api_key': 'k', 'Token': 't'})
        self.assertEqual(cleaned['user'], 'bob')
        for field in ('password', 'ftp_password', 'api_key', 'Token'):
            self.assertEqual(cleaned[field], '***')

    def test_known_secrets_are_masked_wherever_they_appear(self):
        secret = 'super-secret-bearer-token'
        cleaned = redact({'note': 'header was Bearer %s' % secret}, [secret])
        self.assertNotIn(secret, cleaned['note'])

    def test_nested_structures_are_walked(self):
        cleaned = redact({'outer': {'inner': {'password': 'p'}}})
        self.assertEqual(cleaned['outer']['inner']['password'], '***')

    def test_long_values_are_truncated(self):
        cleaned = redact({'body': 'x' * 5000})
        self.assertLess(len(cleaned['body']), 600)
        self.assertTrue(cleaned['body'].endswith('<truncated>'))

    def test_short_secrets_are_not_used_for_substring_matching(self):
        # Masking a 3-character secret would shred unrelated text.
        cleaned = redact({'note': 'the cat sat'}, ['cat'])
        self.assertEqual(cleaned['note'], 'the cat sat')


class AuditFileTest(unittest.TestCase):
    def setUp(self):
        self.panel = FakePanel()
        self.config = cfg.load()
        self.config['auth']['token'] = 'bearer-token-value-1234'
        self.audit = AuditLog(self.config)

    def tearDown(self):
        self.panel.stop()

    def test_entries_are_one_json_object_per_line_newest_first(self):
        self.audit.record(tool='a', outcome='ok')
        self.audit.record(tool='b', outcome='ok')
        entries = self.audit.tail()
        self.assertEqual([e['tool'] for e in entries], ['b', 'a'])
        self.assertIn('time', entries[0])

    def test_the_log_file_is_not_world_readable(self):
        self.audit.record(tool='a', outcome='ok')
        mode = os.stat(cfg.audit_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_search_filters_entries(self):
        self.audit.record(tool='site_create', outcome='ok')
        self.audit.record(tool='database_create', outcome='ok')
        self.assertEqual(len(self.audit.tail(contains='site_')), 1)

    def test_rotation_keeps_history_readable(self):
        self.audit.max_bytes = 200
        for index in range(60):
            self.audit.record(tool='tool%d' % index, outcome='ok')
        self.assertTrue(os.path.exists(cfg.audit_path() + '.1'))
        self.assertTrue(self.audit.tail(limit=5))

    def test_clear_removes_every_rotation(self):
        self.audit.max_bytes = 200
        for index in range(60):
            self.audit.record(tool='tool%d' % index, outcome='ok')
        self.audit.clear()
        self.assertEqual(self.audit.tail(), [])


class AuditIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.panel = FakePanel()
        basic_routes(self.panel)
        self.panel.route('/database', 'AddDatabase', {'status': True, 'msg': 'ok'})
        config = cfg.load()
        config['auth']['token'] = 'bearer-token-value-1234'
        cfg.save(config)
        self.audit = AuditLog(cfg.load())
        self.server = build_test_server({'write': True}, audit=self.audit)
        self.ctx = RequestContext(ERA_MODERN, peer='10.1.2.3',
                                  client_info={'name': 'TestClient', 'version': '9'})

    def tearDown(self):
        self.panel.stop()

    def _call(self, name, arguments):
        return self.server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                     'params': {'name': name, 'arguments': arguments}},
                                    self.ctx)['result']

    def test_a_successful_call_is_recorded_with_who_and_how_long(self):
        self._call('site_list', {})
        entry = self.audit.tail()[0]
        self.assertEqual(entry['tool'], 'site_list')
        self.assertEqual(entry['outcome'], 'ok')
        self.assertEqual(entry['client'], 'TestClient/9')
        self.assertEqual(entry['peer'], '10.1.2.3')
        self.assertIn('duration_ms', entry)

    def test_a_password_argument_never_lands_in_the_log(self):
        self._call('database_create', {'name': 'app', 'password': 'S3cret-Passw0rd'})
        with open(cfg.audit_path(), encoding='utf-8') as fp:
            raw = fp.read()
        self.assertNotIn('S3cret-Passw0rd', raw)
        self.assertEqual(json.loads(raw.splitlines()[-1])['arguments']['password'], '***')

    def test_a_refused_call_is_recorded_too(self):
        self._call('site_delete', {'site': 'example.com'})
        entry = self.audit.tail()[0]
        self.assertEqual(entry['tool'], 'site_delete')
        self.assertEqual(entry['outcome'], 'denied')


if __name__ == '__main__':
    unittest.main()
