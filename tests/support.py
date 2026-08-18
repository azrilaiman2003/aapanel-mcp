# coding: utf-8
"""Test scaffolding: a fake aaPanel, and a temporary plugin home.

The fake panel implements the real authentication rule from class/common.py —
request_token == md5(request_time + md5(api_sk)) — so the client is exercised against
the same contract the live panel enforces, without needing aaPanel installed.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_SRC = os.path.join(REPO_ROOT, 'plugin', 'aapanel_mcp')
if PLUGIN_SRC not in sys.path:
    sys.path.insert(0, PLUGIN_SRC)

PANEL_VERSION = '8.21.0'


def md5(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_POST(self):
        panel = self.server.panel
        path = self.path.split('?', 1)[0]
        # keep_blank_values matches werkzeug, which is what the real panel parses with:
        # an empty-string parameter must arrive as an empty string, not vanish.
        query = (urllib.parse.parse_qs(self.path.split('?', 1)[1], keep_blank_values=True)
                 if '?' in self.path else {})
        length = int(self.headers.get('Content-Length') or 0)
        form = (urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8'),
                                      keep_blank_values=True) if length else {})

        params = {k: v[0] for k, v in query.items()}
        params.update({k: v[0] for k, v in form.items()})

        if not panel.api_open:
            return self._json(200, {'status': False, 'msg': 'api closed'})

        expected = md5(params.get('request_time', '') + md5(panel.api_sk))
        if params.get('request_token') != expected:
            # The real panel answers a bad token with its login page.
            return self._html(200, '<html><body>login</body></html>')

        panel.calls.append({'path': path, 'params': params})
        handler = panel.routes.get((path, params.get('action'))) or panel.routes.get((path, None))
        if handler is None:
            return self._json(404, {'status': False, 'msg': 'not found'})
        result = handler(params) if callable(handler) else handler
        return self._json(200, result)

    def _json(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status, text):
        body = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakePanel:
    """A stand-in aaPanel: HTTP endpoint plus the on-disk layout the client reads."""

    def __init__(self, api_sk='test-secret-key', api_open=True, limit_addr=('127.0.0.1',)):
        self.api_sk = api_sk
        self.api_open = api_open
        self.limit_addr = list(limit_addr)
        self.routes = {}
        self.calls = []

        self.root = tempfile.mkdtemp(prefix='aapanel-mcp-test-')
        self.panel_home = os.path.join(self.root, 'panel')
        self.plugin_home = os.path.join(self.panel_home, 'plugin', 'aapanel_mcp')
        os.makedirs(os.path.join(self.panel_home, 'data'))
        os.makedirs(os.path.join(self.panel_home, 'config'))
        os.makedirs(os.path.join(self.panel_home, 'class'))
        os.makedirs(os.path.join(self.plugin_home, 'data'))

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self._server.panel = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        self._write(os.path.join(self.panel_home, 'data', 'port.pl'), str(self.port))
        self._write(os.path.join(self.panel_home, 'class', 'common.py'),
                    "class panelSetup:\n    def init(self):\n        g.version = '%s'\n"
                    % PANEL_VERSION)
        self.write_api_config()

        self._saved_env = {key: os.environ.get(key)
                           for key in ('AAPANEL_PANEL_HOME', 'AAPANEL_MCP_HOME')}
        os.environ['AAPANEL_PANEL_HOME'] = self.panel_home
        os.environ['AAPANEL_MCP_HOME'] = self.plugin_home

    @staticmethod
    def _write(path, text):
        with open(path, 'w', encoding='utf-8') as fp:
            fp.write(text)

    def write_api_config(self):
        payload = {'open': self.api_open, 'token': md5(self.api_sk),
                   'limit_addr': self.limit_addr}
        self._write(os.path.join(self.panel_home, 'config', 'api.json'), json.dumps(payload))

    def close_api(self):
        self.api_open = False
        self.write_api_config()

    def route(self, path, action, result):
        """Register a canned response. `action=None` matches any action on that path."""
        self.routes[(path, action)] = result

    def install_plugin(self, name, main_source=''):
        """Create a plugin directory so plugin-presence checks find it."""
        directory = os.path.join(self.panel_home, 'plugin', name)
        os.makedirs(directory, exist_ok=True)
        self._write(os.path.join(directory, 'info.json'),
                    json.dumps({'name': name, 'title': name, 'versions': '1.0'}))
        if main_source:
            self._write(os.path.join(directory, '%s_main.py' % name), main_source)
        return directory

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.root, ignore_errors=True)


def build_test_server(tiers=None, audit=None):
    """An McpServer wired to the fake panel, with the requested tiers enabled."""
    from mcpd import config as cfg
    from mcpd.panel_client import PanelClient
    from mcpd.protocol import McpServer
    from mcpd.tools import build_registry

    config = cfg.load()
    if tiers:
        config['permissions']['tiers'].update(tiers)
        cfg.save(config)

    def provider():
        return cfg.load()

    return McpServer(provider, build_registry(), PanelClient(timeout=5), audit)


def basic_routes(panel):
    """The handful of endpoints most tests need."""
    panel.route('/system', 'GetLoadAverage', {'one': 0.1, 'five': 0.2, 'fifteen': 0.3})
    panel.route('/system', 'GetSystemTotal', {'cpuNum': 4, 'memTotal': 8192})
    panel.route('/system', 'GetDiskInfo', [{'path': '/', 'size': ['50G', '20G', '30G', '40%']}])
    panel.route('/system', 'GetNetWork', {'up': 1, 'down': 2})
    panel.route('/data', 'getData', lambda params: {
        'data': [
            {'id': 1, 'name': 'example.com', 'path': '/www/wwwroot/example.com',
             'status': '1', 'ps': 'demo', 'php_version': '82'},
            {'id': 2, 'name': 'demo.test', 'path': '/www/wwwroot/demo.test',
             'status': '1', 'ps': '', 'php_version': '74'},
        ] if params.get('table') == 'sites' else [
            {'id': 7, 'name': 'appdb', 'username': 'appdb', 'ps': 'app database'},
        ] if params.get('table') == 'databases' else [],
        'page': '<div>total 2</div>',
    })
