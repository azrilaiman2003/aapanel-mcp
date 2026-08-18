# coding: utf-8
"""Streamable HTTP transport.

One POST endpoint, as the 2026-07-28 transport requires. The extras that revision
removed — GET streams, `Mcp-Session-Id`, `Last-Event-ID` — are still tolerated for
clients that speak a legacy revision, because a panel plugin has to work with whatever
MCP client the administrator already has installed.

Security in front of the protocol, in this order: source IP, `Origin`, bearer token,
body size, rate limit. Only then is the JSON even parsed.
"""

import base64
import errno
import hmac
import json
import os
import socketserver
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import PROTOCOL_MODERN, SUPPORTED_PROTOCOLS, SERVER_NAME, __version__
from . import config as cfg
from .protocol import (ERA_LEGACY, ERA_MODERN, ProtocolError, RequestContext,
                       CODE_PARSE, CODE_INVALID_REQUEST, META_CLIENT_CAPS, META_CLIENT_INFO,
                       META_PROTOCOL, header_mismatch, meta_of, notification,
                       subscription_filter, unsupported_version)

BASE64_PREFIX = '=?base64?'
BASE64_SUFFIX = '?='
KEEPALIVE_SECONDS = 15
NAMED_METHODS = {'tools/call': 'name', 'resources/read': 'uri', 'prompts/get': 'name'}


def decode_header_value(value):
    """Undo the `=?base64?...?=` sentinel the spec uses for non-ASCII header values."""
    if isinstance(value, str) and value.startswith(BASE64_PREFIX) and value.endswith(BASE64_SUFFIX):
        payload = value[len(BASE64_PREFIX):-len(BASE64_SUFFIX)]
        try:
            return base64.b64decode(payload).decode('utf-8')
        except Exception:
            return value
    return value


class RateLimiter:
    """Fixed-window counter per client address."""

    def __init__(self, per_minute):
        self.per_minute = max(0, int(per_minute or 0))
        self._lock = threading.Lock()
        self._windows = {}

    def allow(self, key):
        if not self.per_minute:
            return True
        now = int(time.time() // 60)
        with self._lock:
            window, count = self._windows.get(key, (now, 0))
            if window != now:
                window, count = now, 0
            count += 1
            self._windows[key] = (window, count)
            if len(self._windows) > 4096:
                self._windows = {k: v for k, v in self._windows.items() if v[0] == now}
            return count <= self.per_minute


def ensure_self_signed(cert_path, key_path, common_name='aapanel-mcp'):
    """Generate a long-lived self-signed pair with openssl if it isn't there yet.

    Python cannot mint a certificate without a third-party library, and openssl is on
    every machine that runs aaPanel.
    """
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return True
    os.makedirs(os.path.dirname(cert_path), mode=0o700, exist_ok=True)
    command = [
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '3650',
        '-keyout', key_path, '-out', cert_path,
        '-subj', '/CN=%s' % common_name,
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    os.chmod(key_path, 0o600)
    os.chmod(cert_path, 0o644)
    return True


def build_ssl_context(config):
    """None when TLS is off, otherwise a configured context."""
    tls = config['bind']['tls']
    mode = tls.get('mode', 'off')
    if mode == 'off':
        return None

    if mode == 'panel':
        cert = os.path.join(cfg.panel_home(), 'ssl', 'certificate.pem')
        key = os.path.join(cfg.panel_home(), 'ssl', 'privateKey.pem')
    elif mode == 'custom':
        cert, key = tls.get('cert', ''), tls.get('key', '')
    else:
        cert = os.path.join(cfg.data_dir(), 'self_signed.pem')
        key = os.path.join(cfg.data_dir(), 'self_signed.key')
        if not ensure_self_signed(cert, key):
            raise RuntimeError('could not generate a self-signed certificate (is openssl installed?)')

    if not (cert and key and os.path.exists(cert) and os.path.exists(key)):
        raise RuntimeError('TLS mode "%s" needs a certificate and key (looked for %s)' % (mode, cert))

    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert, key)
    return context


class McpRequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = '%s/%s' % (SERVER_NAME, __version__)
    sys_version = ''

    # -------------------------------------------------------------- plumbing

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        self.app.log('http', fmt % args)

    def _client_ip(self):
        ip = self.client_address[0]
        if self.app.config['bind'].get('trust_forwarded'):
            forwarded = self.headers.get('X-Forwarded-For')
            if forwarded:
                return forwarded.split(',')[0].strip()
        return ip

    def _send(self, status, body=b'', content_type='application/json', extra=None):
        self.send_response(status)
        if body:
            self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status, payload, extra=None):
        self._send(status, json.dumps(payload).encode('utf-8'), 'application/json', extra)

    def _send_error_response(self, status, code, message, data=None, request_id=None, extra=None):
        error = {'code': code, 'message': message}
        if data is not None:
            error['data'] = data
        payload = {'jsonrpc': '2.0', 'error': error}
        if request_id is not None:
            payload['id'] = request_id
        self._send_json(status, payload, extra)

    # ------------------------------------------------------------- gatekeeping

    def _gate(self):
        """Returns True when the request may proceed; has already answered when False."""
        config = self.app.config
        peer = self._client_ip()

        allowlist = config['auth'].get('ip_allowlist') or []
        if allowlist and peer not in allowlist:
            self.app.log('deny', 'ip %s not in allowlist' % peer)
            self._send(403, b'{"error":"forbidden"}')
            return False

        origin = self.headers.get('Origin')
        if origin:
            # Non-browser clients send no Origin at all. A browser one is only allowed
            # if the admin listed it; this is the DNS-rebinding guard the spec requires.
            allowed = config['auth'].get('origin_allowlist') or []
            if origin not in allowed:
                self.app.log('deny', 'origin %s rejected' % origin)
                self._send(403, b'{"error":"origin not allowed"}')
                return False

        expected = config['auth'].get('token') or ''
        supplied = ''
        header = self.headers.get('Authorization') or ''
        if header.lower().startswith('bearer '):
            supplied = header[7:].strip()
        # compare_digest so a wrong token cannot be narrowed down by timing.
        if not supplied or not hmac.compare_digest(supplied, expected):
            self.app.log('deny', 'bad or missing bearer token from %s' % peer)
            self._send(401, b'{"error":"unauthorized"}',
                       extra={'WWW-Authenticate': 'Bearer realm="aapanel-mcp"'})
            return False

        if not self.app.rate_limiter.allow(peer):
            self._send(429, b'{"error":"rate limit exceeded"}', extra={'Retry-After': '60'})
            return False

        return True

    def _read_body(self):
        limit = int(self.app.config['limits'].get('max_body_bytes') or 1048576)
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length > limit:
            self._send(413, b'{"error":"request too large"}')
            return None
        return self.rfile.read(length) if length else b''

    # ----------------------------------------------------------------- methods

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/healthz':
            # Unauthenticated on purpose and deliberately contentless: it exists so a
            # reverse proxy or the plugin UI can tell "listening" from "dead".
            self._send_json(200, {'status': 'ok', 'name': SERVER_NAME, 'version': __version__})
            return
        if path != self.app.endpoint_path:
            self._send(404, b'{"error":"not found"}')
            return
        if not self._gate():
            return
        # 2026-07-28 removed the GET stream; legacy clients still open one and expect it
        # to stay quiet until something happens.
        self._stream_legacy_get()

    def do_DELETE(self):
        # Legacy session teardown. There are no sessions to tear down, so just say yes.
        if self.path.split('?', 1)[0] != self.app.endpoint_path:
            self._send(404, b'{"error":"not found"}')
            return
        if not self._gate():
            return
        self._send(200, b'{"status":"ok"}')

    def do_POST(self):
        if self.path.split('?', 1)[0] != self.app.endpoint_path:
            self._send(404, b'{"error":"not found"}')
            return
        if not self._gate():
            return

        raw = self._read_body()
        if raw is None:
            return
        try:
            message = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            self._send_error_response(400, CODE_PARSE, 'Invalid JSON')
            return
        if not isinstance(message, dict):
            # Batching was removed from MCP; a list is a client bug worth naming.
            self._send_error_response(400, CODE_INVALID_REQUEST,
                                      'Expected a single JSON-RPC message, not an array')
            return

        request_id = message.get('id')
        try:
            ctx = self._context_for(message)
        except ProtocolError as exc:
            self._send_error_response(exc.http_status, exc.code, exc.message, exc.data, request_id)
            return

        if request_id is None:
            # A notification: nothing to answer, but say we took it.
            try:
                self.app.server.dispatch(message, ctx)
            except ProtocolError:
                pass
            self._send(202)
            return

        if message.get('method') == 'subscriptions/listen':
            self._stream_subscription(message, ctx)
            return

        try:
            response = self.app.server.dispatch(message, ctx)
        except ProtocolError as exc:
            self._send_error_response(exc.http_status, exc.code, exc.message, exc.data, request_id)
            return
        except Exception as exc:
            self.app.log('error', 'dispatch failed: %s' % exc)
            self._send_error_response(500, -32603, 'Internal error: %s' % exc, None, request_id)
            return

        self._send_json(200, response)

    # ------------------------------------------------------------------- era

    def _context_for(self, message):
        """Decide which protocol era this request belongs to and validate accordingly."""
        method = message.get('method') or ''
        meta = meta_of(message)
        header_version = self.headers.get('MCP-Protocol-Version')
        body_version = meta.get(META_PROTOCOL)
        peer = self._client_ip()

        if method == 'initialize':
            version = message.get('params', {}).get('protocolVersion') or ''
            return RequestContext(ERA_LEGACY, version, peer=peer)

        version = header_version or body_version
        if version is None:
            # No version anywhere: the transport spec lets a server read this as the
            # oldest Streamable HTTP revision rather than rejecting it outright.
            return RequestContext(ERA_LEGACY, '2025-03-26',
                                  client_info=meta.get(META_CLIENT_INFO),
                                  client_capabilities=meta.get(META_CLIENT_CAPS), peer=peer)

        if version not in SUPPORTED_PROTOCOLS:
            raise unsupported_version(version)

        if version != PROTOCOL_MODERN:
            return RequestContext(ERA_LEGACY, version,
                                  client_info=meta.get(META_CLIENT_INFO),
                                  client_capabilities=meta.get(META_CLIENT_CAPS), peer=peer)

        self._validate_modern_headers(message, method, header_version, body_version)
        return RequestContext(ERA_MODERN, version,
                              client_info=meta.get(META_CLIENT_INFO),
                              client_capabilities=meta.get(META_CLIENT_CAPS), peer=peer)

    def _validate_modern_headers(self, message, method, header_version, body_version):
        """Headers mirror body fields; a mismatch is a -32020, per the transport spec."""
        if not header_version:
            raise header_mismatch('the MCP-Protocol-Version header is required')
        if body_version and body_version != header_version:
            raise header_mismatch('MCP-Protocol-Version header value %r does not match body value %r'
                                  % (header_version, body_version))

        header_method = self.headers.get('Mcp-Method')
        if not header_method:
            raise header_mismatch('the Mcp-Method header is required')
        if header_method != method:
            raise header_mismatch('Mcp-Method header value %r does not match body value %r'
                                  % (header_method, method))

        name_field = NAMED_METHODS.get(method)
        if not name_field:
            return
        body_name = (message.get('params') or {}).get(name_field)
        header_name = self.headers.get('Mcp-Name')
        if body_name is None:
            return
        if header_name is None:
            raise header_mismatch('the Mcp-Name header is required for %s' % method)
        if decode_header_value(header_name) != body_name:
            raise header_mismatch('Mcp-Name header value %r does not match body value %r'
                                  % (header_name, body_name))

    # --------------------------------------------------------------- streaming

    def _open_stream(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache, no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

    def _emit(self, payload):
        self.wfile.write(('data: %s\n\n' % json.dumps(payload)).encode('utf-8'))
        self.wfile.flush()

    def _stream_subscription(self, message, ctx):
        """subscriptions/listen: acknowledge, then push tools/list_changed as it happens."""
        params = message.get('params') or {}
        agreed = subscription_filter(params)
        subscription_id = message.get('id')
        self._open_stream()
        try:
            self._emit(notification('notifications/subscriptions/acknowledged',
                                    {'notifications': agreed}, subscription_id))
            signature = self.app.server.tools_signature()
            last_keepalive = time.time()
            while not self.app.stopping:
                time.sleep(1.0)
                if agreed.get('toolsListChanged'):
                    current = self.app.server.tools_signature()
                    if current != signature:
                        signature = current
                        self._emit(notification('notifications/tools/list_changed', None,
                                                subscription_id))
                if time.time() - last_keepalive >= KEEPALIVE_SECONDS:
                    self.wfile.write(b':\r\n')
                    self.wfile.flush()
                    last_keepalive = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _stream_legacy_get(self):
        """The pre-2026 standalone SSE channel: stay open, send keepalives.

        This revision has no server-initiated messages to put on it, but legacy clients
        open it during startup and treat a closed stream as a failed connection.
        """
        self._open_stream()
        last_keepalive = 0.0
        try:
            while not self.app.stopping:
                if time.time() - last_keepalive >= KEEPALIVE_SECONDS:
                    self.wfile.write(b':\r\n')
                    self.wfile.flush()
                    last_keepalive = time.time()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class _ThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Long-lived SSE streams must not keep the process alive past a stop request.
    block_on_close = False

    def __init__(self, address, handler, app):
        self.app = app
        if ':' in address[0]:
            self.address_family = socketserver.socket.AF_INET6
        super().__init__(address, handler)


class HttpApp:
    """Wires config, the MCP server and the socket together."""

    def __init__(self, config_provider, mcp_server, logger=None):
        self._config_provider = config_provider
        self.server = mcp_server
        self._logger = logger
        self.stopping = False
        config = config_provider()
        self.endpoint_path = config['bind'].get('path') or '/mcp'
        self.rate_limiter = RateLimiter(config['limits'].get('rate_per_minute'))
        self._httpd = None

    @property
    def config(self):
        return self._config_provider()

    def log(self, kind, message):
        if self._logger:
            self._logger(kind, message)

    def serve_forever(self):
        config = self.config
        host = config['bind']['host']
        port = int(config['bind']['port'])

        # Settle TLS before claiming the port. A certificate problem should fail while
        # the socket is still unbound, otherwise systemd's restart loop leaves the port
        # flickering open and shut and the admin sees an intermittent "connection
        # refused" with nothing obviously wrong.
        try:
            context = build_ssl_context(config)
        except (RuntimeError, ssl.SSLError, OSError) as exc:
            self.log('error', 'not starting: %s' % exc)
            raise

        try:
            self._httpd = _ThreadingHTTPServer((host, port), McpRequestHandler, self)
        except OSError as exc:
            # The two that actually happen: the address is not on any interface (a
            # public IP behind NAT), or something already holds the port. Both look
            # identical from outside, so name them here.
            self.log('error', 'cannot bind %s:%s - %s. %s' % (
                host, port, exc,
                'Is that address on this machine? Use 0.0.0.0 to listen on every '
                'interface.' if exc.errno == errno.EADDRNOTAVAIL else
                'Another process is using that port.' if exc.errno == errno.EADDRINUSE else ''))
            raise

        try:
            if context is not None:
                self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
            self.log('start', 'listening on %s:%s%s (tls=%s)'
                     % (host, port, self.endpoint_path, config['bind']['tls']['mode']))
            self._httpd.serve_forever(poll_interval=0.5)
        finally:
            self._httpd.server_close()

    def stop(self):
        self.stopping = True
        if self._httpd:
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()
