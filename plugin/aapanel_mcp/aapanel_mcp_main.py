# coding: utf-8
"""Panel-side control surface.

aaPanel calls the methods of this class from the plugin UI:

    /plugin?action=a&name=aapanel_mcp&s=<method>

Nothing here serves MCP. This module configures the daemon, starts and stops it, and
reads back its state — the protocol itself lives in the separate service process, which
is why a panel restart does not interrupt a running agent and a daemon crash cannot take
the panel down with it.
"""

import json
import os
import subprocess
import sys
import time

PLUGIN_PATH = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_PATH not in sys.path:
    sys.path.insert(0, PLUGIN_PATH)

from mcpd import __version__                                    # noqa: E402
from mcpd import config as cfg                                  # noqa: E402
from mcpd import permissions                                    # noqa: E402
from mcpd.audit import AuditLog                                 # noqa: E402
from mcpd.panel_client import PanelClient                       # noqa: E402
from mcpd.tools import build_registry                           # noqa: E402

SERVICE_NAME = 'aapanel-mcp'
SYSTEMD_UNIT = '/etc/systemd/system/%s.service' % SERVICE_NAME
INIT_SCRIPT = '/etc/init.d/aapanel_mcp'


def _arg(get, name, default=None):
    """Read one request parameter, whatever flavour of dict-like the panel handed us."""
    try:
        value = getattr(get, name)
    except AttributeError:
        try:
            value = get[name]
        except (KeyError, TypeError):
            return default
    return default if value is None else value


def _ok(message, **extra):
    payload = {'status': True, 'msg': message}
    payload.update(extra)
    return payload


def _fail(message, **extra):
    payload = {'status': False, 'msg': message}
    payload.update(extra)
    return payload


def _shell(command, timeout=60):
    try:
        done = subprocess.run(command, shell=True, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return done.returncode, done.stdout.decode('utf-8', 'replace').strip()
    except subprocess.SubprocessError as exc:
        return 1, str(exc)


class aapanel_mcp_main:

    def __init__(self):
        self.panel = PanelClient()
        self._registry = None

    @property
    def registry(self):
        if self._registry is None:
            self._registry = build_registry()
        return self._registry

    # --------------------------------------------------------------- service

    @staticmethod
    def _has_systemd():
        return os.path.exists('/usr/bin/systemctl') or os.path.exists('/bin/systemctl')

    def _service(self, action):
        if self._has_systemd() and os.path.exists(SYSTEMD_UNIT):
            return _shell('systemctl %s %s' % (action, SERVICE_NAME))
        if os.path.exists(INIT_SCRIPT):
            return _shell('%s %s' % (INIT_SCRIPT, action))
        return 1, 'No service definition found. Reinstall the plugin to recreate it.'

    def _pid(self):
        try:
            with open(cfg.pid_path(), encoding='utf-8') as fp:
                pid = int(fp.read().strip())
        except (OSError, ValueError):
            return 0
        return pid if os.path.exists('/proc/%d' % pid) else 0

    def service_status(self, get=None):
        pid = self._pid()
        state = {
            'running': bool(pid),
            'pid': pid,
            'manager': 'systemd' if (self._has_systemd() and os.path.exists(SYSTEMD_UNIT)) else 'init.d',
        }
        if pid:
            try:
                state['uptime_seconds'] = int(time.time() - os.path.getmtime('/proc/%d' % pid))
            except OSError:
                pass
        return state

    def start(self, get=None):
        if self._pid():
            return _ok('The MCP server is already running.')
        code, output = self._service('start')
        time.sleep(1.2)
        if self._pid():
            return _ok('MCP server started.')
        return _fail('The MCP server did not start. %s' % (output or self._log_tail(15)))

    def stop(self, get=None):
        code, output = self._service('stop')
        time.sleep(0.6)
        if self._pid():
            return _fail('The MCP server is still running. %s' % output)
        return _ok('MCP server stopped.')

    def restart(self, get=None):
        self._service('stop')
        time.sleep(0.6)
        return self.start(get)

    def _log_tail(self, lines=200):
        try:
            with open(cfg.daemon_log_path(), encoding='utf-8', errors='replace') as fp:
                return ''.join(fp.readlines()[-lines:])
        except OSError:
            return ''

    def get_logs(self, get=None):
        return {'log': self._log_tail(int(_arg(get, 'lines', 200) or 200))}

    # ---------------------------------------------------------------- status

    def get_status(self, get=None):
        config = cfg.load()
        panel_state = self.panel.status()
        service = self.service_status()
        tiers = config['permissions']['tiers']
        enabled = [tool for tool in self.registry.all()
                   if permissions.tool_enabled(config, tool)]
        return {
            'status': True,
            'version': __version__,
            'service': service,
            'panel_api': panel_state,
            'bind': config['bind'],
            'endpoint': cfg.endpoint_url(config, self._public_host()),
            'tiers': tiers,
            'tool_counts': {
                'total': len(self.registry),
                'enabled': len(enabled),
                'by_domain': self._counts_by_domain(config),
            },
            'stdio_command': os.path.join(PLUGIN_PATH, 'bin', 'aapanel-mcp-stdio'),
            'audit_enabled': config['audit']['enabled'],
            'warnings': self._warnings(config),
        }

    @staticmethod
    def _warnings(config):
        """Configurations that run without error but cannot do what was asked of them."""
        found = []
        bind = config['bind']
        if bind['mode'] == 'bound' and _is_loopback(bind['host']):
            found.append('This is set to listen on a network port, but the listen address '
                         'is %s — only this machine can reach that. Remote clients will get '
                         '"connection refused". Set the listen address to 0.0.0.0.'
                         % bind['host'])
        if bind['mode'] == 'bound' and bind['tls']['mode'] == 'off':
            found.append('The port is reachable over the network with TLS off, so the access '
                         'token crosses the network in clear text.')
        return found

    def _counts_by_domain(self, config):
        counts = {}
        for tool in self.registry.all():
            bucket = counts.setdefault(tool.domain, {'total': 0, 'enabled': 0})
            bucket['total'] += 1
            if permissions.tool_enabled(config, tool):
                bucket['enabled'] += 1
        return counts

    @staticmethod
    def _public_host():
        for path in ('/www/server/panel/data/iplist.txt',):
            try:
                with open(path, encoding='utf-8') as fp:
                    value = fp.read().strip()
                    if value:
                        return value.split('\n')[0]
            except OSError:
                continue
        return ''

    # ------------------------------------------------------------ panel API

    def enable_panel_api(self, get=None):
        """Switch on the panel's API and allow 127.0.0.1, generating a key if needed.

        The daemon authenticates with the md5 of this key, which it reads straight from
        api.json — so the admin never has to copy a secret anywhere. Done here rather
        than in the daemon because writing panel config is the panel's business, and
        because it should be an explicit click, not a side effect of starting a server.
        """
        path = self.panel.api_config_path
        config = {'open': False, 'token': '', 'limit_addr': []}
        if os.path.exists(path):
            try:
                with open(path, encoding='utf-8') as fp:
                    config = json.load(fp)
            except (ValueError, OSError):
                pass

        import hashlib
        import secrets
        generated = None
        if not config.get('token'):
            generated = secrets.token_urlsafe(24)
            config['token'] = hashlib.md5(generated.encode('utf-8')).hexdigest()
        config['open'] = True
        limit = config.get('limit_addr') or []
        if '127.0.0.1' not in limit:
            limit.append('127.0.0.1')
        config['limit_addr'] = limit

        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            return _fail('The panel config directory %s does not exist.' % directory)
        with open(path, 'w', encoding='utf-8') as fp:
            json.dump(config, fp)
        os.chmod(path, 0o600)

        state = self.panel.status()
        message = 'Panel API enabled for 127.0.0.1.'
        if generated:
            message += ' A new API key was generated; it is shown in Settings -> API.'
        if not state['ok']:
            return _fail('Panel API still unusable: %s' % state['reason'])
        return _ok(message, api_key=generated or '')

    def test_panel_api(self, get=None):
        """Prove the daemon's credentials work by making a real call."""
        try:
            result = self.panel.call('/system', 'GetLoadAverage')
        except Exception as exc:
            return _fail('Panel API call failed: %s' % exc)
        return _ok('Panel API is working.', sample=result)

    # ----------------------------------------------------------------- config

    def get_config(self, get=None):
        config = cfg.load()
        config['_endpoint'] = cfg.endpoint_url(config, self._public_host())
        return config

    def save_config(self, get=None):
        """Accepts a JSON object of changes and merges it over the stored config."""
        raw = _arg(get, 'data', '')
        if not raw:
            return _fail('No configuration supplied.')
        try:
            changes = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except ValueError:
            return _fail('The configuration is not valid JSON.')

        problem = _validate(changes)
        if problem:
            return _fail(problem)

        config = cfg.load()
        _merge_into(config, changes)
        # Something in front of us sets X-Forwarded-For only in proxy mode.
        config['bind']['trust_forwarded'] = config['bind']['mode'] == 'proxy'
        if config['bind']['mode'] in ('localhost', 'proxy'):
            config['bind']['host'] = '127.0.0.1'
        elif _is_loopback(config['bind']['host']):
            # Checked after the merge, not in _validate, so it holds however few keys the
            # caller sent. Bound mode on a loopback address is silently identical to
            # localhost mode: it listens, it answers locally, and every remote client
            # gets "connection refused" with nothing in the log to explain it.
            return _fail('The listen address is %s, which only this machine can reach, so '
                         'nothing outside would be able to connect. Use 0.0.0.0 to listen '
                         'on every interface, or choose "This server only" if you meant to '
                         'keep it local.' % config['bind']['host'])
        cfg.save(config)
        restarted = bool(self._pid()) and self.restart()['status']
        return _ok('Settings saved.%s' % (' The server was restarted.' if restarted else ''),
                   config=config)

    def regenerate_token(self, get=None):
        config = cfg.load()
        config['auth']['token'] = cfg.new_secret()
        cfg.save(config)
        if self._pid():
            self.restart()
        return _ok('A new access token was generated. Update your MCP client.',
                   token=config['auth']['token'])

    # ------------------------------------------------------------ permissions

    def get_tools(self, get=None):
        config = cfg.load()
        tools = []
        for tool in self.registry.all():
            tools.append({
                'name': tool.name,
                'title': tool.title,
                'description': tool.description,
                'domain': tool.domain,
                'tier': tool.tier,
                'available': tool.is_available(self.panel),
                'enabled': permissions.tool_enabled(config, tool),
                'override': config['permissions']['tools'].get(tool.name),
            })
        return {'status': True, 'tiers': config['permissions']['tiers'], 'tools': tools}

    def set_tier(self, get=None):
        tier = _arg(get, 'tier', '')
        if tier not in cfg.TIERS:
            return _fail('Unknown tier "%s".' % tier)
        enabled = str(_arg(get, 'enabled', '0')) in ('1', 'true', 'True')
        config = cfg.load()
        config['permissions']['tiers'][tier] = enabled
        cfg.save(config)
        return _ok('The %s tier is now %s.' % (tier, 'enabled' if enabled else 'disabled'),
                   tiers=config['permissions']['tiers'])

    def set_tool(self, get=None):
        name = _arg(get, 'tool', '')
        if not self.registry.get(name):
            return _fail('Unknown tool "%s".' % name)
        state = str(_arg(get, 'state', 'default'))
        config = cfg.load()
        overrides = config['permissions']['tools']
        if state in ('default', ''):
            overrides.pop(name, None)
        else:
            overrides[name] = state in ('1', 'true', 'True', 'on')
        cfg.save(config)
        return _ok('Updated %s.' % name, override=overrides.get(name))

    def apply_preset(self, get=None):
        """Three postures worth one click: read only, everything but shell, everything."""
        preset = _arg(get, 'preset', 'read_only')
        presets = {
            'read_only': {'read': True, 'write': False, 'destructive': False,
                          'shell': False, 'raw': False},
            'manage': {'read': True, 'write': True, 'destructive': True,
                       'shell': False, 'raw': False},
            'full': {'read': True, 'write': True, 'destructive': True,
                     'shell': True, 'raw': True},
        }
        if preset not in presets:
            return _fail('Unknown preset "%s".' % preset)
        config = cfg.load()
        config['permissions']['tiers'] = presets[preset]
        config['permissions']['tools'] = {}
        cfg.save(config)
        return _ok('Applied the "%s" preset.' % preset, tiers=presets[preset])

    # ----------------------------------------------------------------- audit

    def get_audit(self, get=None):
        audit = AuditLog(cfg.load())
        return {'status': True,
                'entries': audit.tail(int(_arg(get, 'limit', 200) or 200),
                                      str(_arg(get, 'search', '') or ''))}

    def clear_audit(self, get=None):
        AuditLog(cfg.load()).clear()
        return _ok('Audit log cleared.')

    # ------------------------------------------------------- client snippets

    def get_client_config(self, get=None):
        config = cfg.load()
        url = cfg.endpoint_url(config, self._public_host())
        token = config['auth']['token']
        stdio = os.path.join(PLUGIN_PATH, 'bin', 'aapanel-mcp-stdio')
        return {
            'status': True,
            'url': url,
            'token': token,
            'claude_code': 'claude mcp add --transport http aapanel %s '
                           '--header "Authorization: Bearer %s"' % (url, token),
            'claude_code_stdio': 'claude mcp add aapanel -- ssh root@%s %s'
                                 % (self._public_host() or '<server>', stdio),
            'json': json.dumps({
                'mcpServers': {
                    'aapanel': {
                        'type': 'http',
                        'url': url,
                        'headers': {'Authorization': 'Bearer %s' % token},
                    }
                }
            }, indent=2),
            'ssh_tunnel': 'ssh -N -L %s:127.0.0.1:%s root@%s'
                          % (config['bind']['port'], config['bind']['port'],
                             self._public_host() or '<server>'),
        }

    # ------------------------------------------------------------- exposure

    def open_firewall(self, get=None):
        config = cfg.load()
        port = config['bind']['port']
        try:
            self.panel.call('/firewall', 'AddAcceptPort', port=port,
                            ps='aaPanel MCP server', type='tcp')
        except Exception as exc:
            return _fail('Could not open port %s: %s' % (port, exc))
        return _ok('Port %s opened in the firewall.' % port)

    def close_firewall(self, get=None):
        config = cfg.load()
        port = config['bind']['port']
        try:
            self.panel.call('/firewall', 'DelAcceptPort', port=port, id='', type='tcp')
        except Exception as exc:
            return _fail('Could not close port %s: %s' % (port, exc))
        return _ok('Port %s closed in the firewall.' % port)

    def setup_proxy_site(self, get=None):
        """Put an aaPanel site in front of the daemon so it can have a real certificate."""
        domain = str(_arg(get, 'domain', '')).strip().lower()
        if not domain or '.' not in domain:
            return _fail('Enter the domain that should serve the MCP endpoint.')
        config = cfg.load()
        port = config['bind']['port']
        path = '/www/wwwroot/%s' % domain

        try:
            sites = self.panel.get_data('sites', limit=1000)
            existing = any(row.get('name') == domain
                           for row in (sites.get('data') if isinstance(sites, dict) else []) or [])
            if not existing:
                webname = json.dumps({'domain': domain, 'domainlist': [], 'count': 0})
                created = self.panel.call('/site', 'AddSite', webname=webname, path=path,
                                          type_id=0, type='PHP', version='00', port=80,
                                          ps='aaPanel MCP endpoint')
                if isinstance(created, dict) and created.get('status') is False:
                    return _fail('Could not create the site: %s' % created.get('msg'))
            proxied = self.panel.call('/site', 'CreateProxy', sitename=domain,
                                      proxyname='aapanel-mcp', proxydir='/',
                                      proxysite='http://127.0.0.1:%s' % port,
                                      todomain='$host', type=1, cache=0,
                                      subfilter=json.dumps([{'sub1': '', 'sub2': ''}]),
                                      advanced=0, cachetime=1)
            if isinstance(proxied, dict) and proxied.get('status') is False:
                return _fail('Could not create the reverse proxy: %s' % proxied.get('msg'))
        except Exception as exc:
            return _fail('Panel call failed: %s' % exc)

        config['bind'].update({'mode': 'proxy', 'host': '127.0.0.1', 'proxy_domain': domain,
                               'trust_forwarded': True})
        config['bind']['tls'] = {'mode': 'off', 'cert': '', 'key': ''}
        cfg.save(config)
        if self._pid():
            self.restart()
        return _ok('%s now proxies to the MCP server. Issue a certificate for it under '
                   'Website -> SSL, then connect to https://%s%s.'
                   % (domain, domain, config['bind']['path']),
                   endpoint='https://%s%s' % (domain, config['bind']['path']))


# ------------------------------------------------------------------ helpers

def _is_loopback(host):
    """True for addresses only this machine can reach.

    An empty host means "every interface" to the socket layer, so it is not loopback.
    """
    return str(host).strip().lower() in ('127.0.0.1', 'localhost', '::1', '[::1]')


def _validate(changes):
    bind = changes.get('bind') or {}
    if 'port' in bind:
        try:
            port = int(bind['port'])
        except (TypeError, ValueError):
            return 'The port must be a number.'
        if not 1 <= port <= 65535:
            return 'The port must be between 1 and 65535.'
        bind['port'] = port
    if 'mode' in bind and bind['mode'] not in ('localhost', 'bound', 'proxy'):
        return 'Unknown bind mode "%s".' % bind['mode']
    tls = bind.get('tls') or {}
    if 'mode' in tls and tls['mode'] not in ('off', 'self_signed', 'panel', 'custom'):
        return 'Unknown TLS mode "%s".' % tls['mode']
    if tls.get('mode') == 'custom':
        for field in ('cert', 'key'):
            value = tls.get(field, '')
            if not value or not os.path.exists(value):
                return 'The custom TLS %s file was not found: %s' % (field, value or '(empty)')
    if 'path' in bind and not str(bind['path']).startswith('/'):
        return 'The endpoint path must start with a slash.'
    return ''


def _merge_into(target, changes):
    for key, value in changes.items():
        if key.startswith('_'):
            continue
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_into(target[key], value)
        else:
            target[key] = value
