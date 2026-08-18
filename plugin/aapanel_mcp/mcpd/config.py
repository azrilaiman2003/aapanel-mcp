# coding: utf-8
"""Plugin paths and the on-disk config file.

Both roots are overridable through the environment so the whole stack can run on a
developer box with no aaPanel installed — that is what the test suite does.
"""

import copy
import json
import os
import secrets
import tempfile

DEFAULT_PANEL_HOME = '/www/server/panel'

# Tiers, coarsest first. A tool declares exactly one; the admin enables tiers in the UI.
TIERS = ('read', 'write', 'destructive', 'shell', 'raw')

DEFAULT_CONFIG = {
    'version': 1,
    'bind': {
        # localhost | bound | proxy
        #   localhost - 127.0.0.1 only, reach it over an SSH tunnel
        #   bound     - listen on `host`, optionally with TLS
        #   proxy     - listen on 127.0.0.1, an aaPanel site reverse-proxies to it
        'mode': 'localhost',
        'host': '127.0.0.1',
        'port': 7801,
        'path': '/mcp',
        'tls': {
            # off | self_signed | panel | custom
            'mode': 'off',
            'cert': '',
            'key': '',
        },
        # Filled in when mode=proxy so the UI can show the public URL and undo the site.
        'proxy_domain': '',
        # Only honour X-Forwarded-For when something in front of us sets it. Turned on
        # automatically in proxy mode, off otherwise, so the IP allowlist cannot be
        # spoofed by a header on a directly-reachable port.
        'trust_forwarded': False,
    },
    'auth': {
        'token': '',
        # Empty list means "any address that got past the bind + firewall".
        'ip_allowlist': [],
        # Browser Origins allowed to talk to the endpoint. Empty means "reject every
        # request that carries an Origin header", which is the safe default for a
        # non-browser API: it blocks DNS-rebinding without blocking real MCP clients.
        'origin_allowlist': [],
    },
    'permissions': {
        'tiers': {
            'read': True,
            'write': False,
            'destructive': False,
            'shell': False,
            'raw': False,
        },
        # Per-tool overrides: {"site_delete": false} wins over its tier.
        'tools': {},
    },
    'confirm': {
        'required': True,
        # Tiers whose tools need a confirm token echoed back before they run.
        'tiers': ['destructive', 'shell'],
        'ttl_seconds': 300,
        'secret': '',
    },
    'audit': {
        'enabled': True,
        'max_bytes': 5 * 1024 * 1024,
        'keep': 3,
    },
    'limits': {
        'rate_per_minute': 240,
        'max_body_bytes': 1024 * 1024,
        'panel_timeout': 60,
    },
    'log_level': 'info',
}


def _package_plugin_dir():
    """Where this package actually lives: .../plugin/aapanel_mcp."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def panel_home():
    """The panel root.

    Derived from our own location rather than assumed, because aaPanel's install path
    is configurable and a plugin that guesses wrong writes its state into the void.
    """
    env = os.environ.get('AAPANEL_PANEL_HOME')
    if env:
        return env
    guess = os.path.dirname(os.path.dirname(_package_plugin_dir()))
    if os.path.isdir(os.path.join(guess, 'class')) and os.path.isdir(os.path.join(guess, 'data')):
        return guess
    return DEFAULT_PANEL_HOME


def plugin_home():
    env = os.environ.get('AAPANEL_MCP_HOME')
    if env:
        return env
    if os.environ.get('AAPANEL_PANEL_HOME'):
        return os.path.join(panel_home(), 'plugin', 'aapanel_mcp')
    return _package_plugin_dir()


def data_dir():
    path = os.path.join(plugin_home(), 'data')
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def config_path():
    return os.path.join(data_dir(), 'config.json')


def audit_path():
    return os.path.join(data_dir(), 'audit.log')


def daemon_log_path():
    return os.path.join(data_dir(), 'daemon.log')


def pid_path():
    return os.path.join(data_dir(), 'daemon.pid')


def _merge(base, override):
    """Deep-merge override into a copy of base, keeping unknown keys from override."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def new_secret():
    return secrets.token_urlsafe(32)


def load(fill_secrets=True):
    """Read config.json, filling in defaults for anything missing.

    Generates the bearer token and confirm secret on first read and writes them back,
    so a fresh install has working credentials without the admin doing anything.
    """
    raw = {}
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as fp:
                raw = json.load(fp)
        except (ValueError, OSError):
            # A corrupt config must not brick the plugin; fall back to defaults and
            # keep the broken file around for the admin to inspect.
            try:
                os.replace(path, path + '.corrupt')
            except OSError:
                pass
            raw = {}

    config = _merge(DEFAULT_CONFIG, raw)
    if fill_secrets:
        changed = False
        if not config['auth'].get('token'):
            config['auth']['token'] = new_secret()
            changed = True
        if not config['confirm'].get('secret'):
            config['confirm']['secret'] = new_secret()
            changed = True
        if changed:
            save(config)
    return config


def save(config):
    """Write config.json atomically with 0600 perms."""
    path = config_path()
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.config-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            json.dump(config, fp, indent=2, sort_keys=False)
            fp.write('\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return config


def update(mutator):
    """Load, apply `mutator(config)`, save, return the saved config."""
    config = load()
    mutator(config)
    return save(config)


def endpoint_url(config, public_host=None):
    """The URL an MCP client should be pointed at."""
    bind = config['bind']
    if bind['mode'] == 'proxy' and bind.get('proxy_domain'):
        return 'https://%s%s' % (bind['proxy_domain'], bind['path'])
    scheme = 'https' if bind['tls']['mode'] != 'off' else 'http'
    host = public_host or bind['host']
    if host in ('0.0.0.0', '::'):
        host = public_host or '<server-ip>'
    if ':' in host and not host.startswith('['):
        host = '[%s]' % host
    return '%s://%s:%s%s' % (scheme, host, bind['port'], bind['path'])
