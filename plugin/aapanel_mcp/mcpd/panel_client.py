# coding: utf-8
"""Talks to the local aaPanel over its own HTTP API.

Why HTTP and not `import panelSite`: the panel's modules do `from BTPanel import
session, cache` and call `public.GetClientIp()`, both of which need a live Flask
request context. Importing them from an outside process works until it doesn't, and
breaks differently on every panel release. The HTTP API is the stable seam.

Authentication (class/common.py: get_sk):

    request_time  = str(int(time.time()))
    request_token = md5(request_time + api_config['token'])

where `api_config['token']` is what /www/server/panel/config/api.json already stores,
namely md5(api_sk). Since this process runs as root on the same box it can read that
file directly — the admin never has to copy an API key anywhere.
"""

import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config as cfg

LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1')

# Panel data tables reachable through /data?action=getData.
DATA_TABLES = ('sites', 'databases', 'ftps', 'crontab', 'logs', 'firewall', 'tasks', 'domain')


class PanelApiError(Exception):
    """A call to the panel could not be made or came back unusable.

    `remediation` is plain text meant for two audiences at once: the plugin UI shows
    it in a banner, and the MCP tool result hands it to the agent so it can tell the
    human what to click.
    """

    def __init__(self, message, remediation='', status=None, body=''):
        super().__init__(message)
        self.message = message
        self.remediation = remediation
        self.status = status
        self.body = body

    def to_dict(self):
        payload = {
            'error': self.message,
            'remediation': self.remediation,
            'http_status': self.status,
        }
        # What the panel actually sent. Without it an agent (or a human reading the
        # audit log) is left guessing at a bare status code.
        if self.body:
            payload['panel_response'] = self.body[:400]
        return payload


def md5(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def _read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fp:
            return fp.read().strip()
    except OSError:
        return ''


def panel_version():
    """Panel version, read out of class/common.py where it is a literal."""
    text = _read(os.path.join(cfg.panel_home(), 'class', 'common.py'))
    match = re.search(r"g\.version\s*=\s*'([^']+)'", text)
    return match.group(1) if match else ''


def _encode_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, separators=(',', ':'))
    return str(value)


class PanelClient:
    def __init__(self, timeout=None):
        self.timeout = timeout or 60
        self._route_prefix = {}
        self._api_config_mtime = None
        self._api_config = None

    # ---------------------------------------------------------------- discovery

    @property
    def api_config_path(self):
        return os.path.join(cfg.panel_home(), 'config', 'api.json')

    def api_config(self):
        """Cached read of config/api.json, invalidated by mtime."""
        path = self.api_config_path
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self._api_config, self._api_config_mtime = None, None
            return None
        if self._api_config is not None and mtime == self._api_config_mtime:
            return self._api_config
        try:
            with open(path, encoding='utf-8') as fp:
                data = json.load(fp)
        except (ValueError, OSError):
            return None
        self._api_config, self._api_config_mtime = data, mtime
        return data

    def base_url(self):
        port = _read(os.path.join(cfg.panel_home(), 'data', 'port.pl')) or '8888'
        scheme = 'https' if os.path.exists(os.path.join(cfg.panel_home(), 'data', 'ssl.pl')) else 'http'
        return '%s://127.0.0.1:%s' % (scheme, port)

    def status(self):
        """Everything the UI needs to render the panel-API health banner."""
        result = {
            'ok': False,
            'base_url': self.base_url(),
            'panel_version': panel_version(),
            'api_open': False,
            'limit_addr': [],
            'reason': '',
            'remediation': '',
        }
        if not os.path.isdir(cfg.panel_home()):
            result['reason'] = 'aaPanel is not installed at %s' % cfg.panel_home()
            return result
        api = self.api_config()
        if api is None:
            result['reason'] = 'The panel API has never been configured.'
            result['remediation'] = 'Click "Enable local panel API" on the Overview tab.'
            return result
        result['api_open'] = bool(api.get('open'))
        result['limit_addr'] = list(api.get('limit_addr') or [])
        if not api.get('open'):
            result['reason'] = 'The panel API is switched off.'
            result['remediation'] = 'Click "Enable local panel API" on the Overview tab, or turn on Settings -> API.'
            return result
        if not api.get('token'):
            result['reason'] = 'The panel API has no key configured.'
            result['remediation'] = 'Click "Enable local panel API" on the Overview tab to generate one.'
            return result
        if result['limit_addr'] and not any(a in result['limit_addr'] for a in LOCAL_HOSTS):
            result['reason'] = '127.0.0.1 is not in the panel API IP allowlist.'
            result['remediation'] = 'Click "Enable local panel API" to add it, or add 127.0.0.1 under Settings -> API.'
            return result
        result['ok'] = True
        return result

    def _require_ready(self):
        state = self.status()
        if not state['ok']:
            raise PanelApiError(state['reason'] or 'The panel API is not usable.',
                                remediation=state['remediation'])
        return state

    # ------------------------------------------------------------------ request

    def _opener(self):
        context = ssl.create_default_context()
        # The panel's own certificate is self-signed and we only ever talk to
        # 127.0.0.1, where the transport is a loopback socket anyway.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    def _post(self, url, fields):
        body = urllib.parse.urlencode(fields).encode('utf-8')
        request = urllib.request.Request(url, data=body, method='POST')
        request.add_header('Content-Type', 'application/x-www-form-urlencoded')
        request.add_header('User-Agent', 'aapanel-mcp')
        request.add_header('Accept', 'application/json')
        try:
            with self._opener().open(request, timeout=self.timeout) as response:
                return response.getcode(), response.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode('utf-8', 'replace')
        except urllib.error.URLError as exc:
            raise PanelApiError(
                'Cannot reach the panel at %s (%s)' % (url, exc.reason),
                remediation='Check that aaPanel is running: `bt status`.')
        except OSError as exc:
            raise PanelApiError('Cannot reach the panel at %s (%s)' % (url, exc))

    def request(self, path, params=None, allow_v2_fallback=True):
        """POST to a panel path, returning the decoded response.

        `path` starts with a slash, e.g. '/site' or '/btdocker/container/get_list'.
        """
        api = self._require_ready()
        api_config = self.api_config()

        fields = {}
        for key, value in (params or {}).items():
            encoded = _encode_value(value)
            if encoded is not None:
                fields[key] = encoded

        request_time = str(int(time.time()))
        fields['request_time'] = request_time
        fields['request_token'] = md5(request_time + api_config['token'])

        prefix = self._route_prefix.get(path, '')
        status, text = self._post(api['base_url'] + prefix + path, fields)

        if status == 404 and allow_v2_fallback and not prefix:
            # aaPanel 7+ also mounts everything under /v2; some builds only have it there.
            status, text = self._post(api['base_url'] + '/v2' + path, fields)
            if status != 404:
                self._route_prefix[path] = '/v2'
        elif status != 404 and path not in self._route_prefix:
            self._route_prefix[path] = prefix

        return self._decode(status, text, path, (params or {}).get('action'))

    def _decode(self, status, text, path, action=None):
        stripped = text.strip()
        # A 404 is not the same thing as "no such route". aaPanel registers a catch-all
        # `@app.errorhandler(Exception)` that, for a request with no *session* (which is
        # every token-authenticated API call), answers with its not-logged-in page —
        # and that page is served as a 404. So any exception inside a panel method
        # surfaces here as an indistinguishable 404. Saying "this panel has no such
        # endpoint" would be a guess, and usually the wrong one.
        if status == 404:
            where = '%s?action=%s' % (path, action) if action else path
            raise PanelApiError(
                'The panel answered 404 for %s.' % where,
                remediation='On aaPanel a 404 means either that the route is absent on '
                            'this version, or that the panel handler raised an exception '
                            '(its error handler returns 404 for API calls, which have no '
                            'session). Check the tail of %s/logs/error.log — if there is a '
                            'traceback timestamped just now, that is the real cause.'
                            % cfg.panel_home(),
                status=status, body=stripped[:400])

        if stripped:
            try:
                return json.loads(stripped)
            except ValueError:
                pass

        # An HTML body here is almost always the login page, which is how the panel
        # answers a request whose token did not verify.
        if '<html' in stripped[:400].lower() or 'login' in stripped[:200].lower():
            raise PanelApiError(
                'The panel rejected the API call and returned its login page.',
                remediation='Re-run "Enable local panel API" on the Overview tab; the stored '
                            'API key may have been rotated in Settings -> API.',
                status=status, body=stripped[:400])
        raise PanelApiError('Unreadable response from the panel (HTTP %s).' % status,
                            status=status, body=stripped[:400])

    # ------------------------------------------------------------- convenience

    def call(self, route, action, **params):
        """`/site?action=AddSite` style call."""
        params['action'] = action
        return self.request(route, params)

    def get_data(self, table, page=1, limit=200, search='', order='', type_=-1):
        """The panel's generic list endpoint, used for sites/databases/ftps/crontab."""
        return self.call('/data', 'getData', table=table, p=page, limit=limit,
                         search=search, order=order, type=type_)

    def plugin_call(self, plugin, method, **params):
        """Reach any installed plugin: /plugin?action=a&name=<plugin>&s=<method>.

        This is what makes the mail server, Docker manager, WAF and every other
        aaPanel app reachable — they are plugins, not core routes.
        """
        params['action'] = 'a'
        params['name'] = plugin
        params['s'] = method
        return self.request('/plugin', params)

    def plugin_installed(self, name):
        return os.path.isfile(os.path.join(cfg.panel_home(), 'plugin', name, 'info.json'))

    def installed_plugins(self):
        """Names of every plugin directory present on disk."""
        root = os.path.join(cfg.panel_home(), 'plugin')
        try:
            return sorted(n for n in os.listdir(root)
                          if os.path.isfile(os.path.join(root, n, 'info.json')))
        except OSError:
            return []


def result_failed(result):
    """True when a panel response is the panel's own `{status: false}` shape."""
    return isinstance(result, dict) and result.get('status') is False


def result_message(result, default=''):
    if isinstance(result, dict):
        for key in ('msg', 'message', 'error'):
            if isinstance(result.get(key), str):
                return result[key]
    return default
