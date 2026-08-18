# coding: utf-8
"""MCP dispatch, dual-era.

Two protocol eras are live in the wild and this server speaks both on the same
endpoint:

* **Modern** (`2026-07-28`): stateless. Every request carries its protocol version,
  client identity and capabilities in `params._meta`; `server/discover` is mandatory;
  results carry a `resultType`. There is no `initialize`, no session, no GET stream.
* **Legacy** (`2025-11-25` and earlier): an `initialize` handshake negotiates the
  version once, results have no `resultType`, and servers may hold a session.

The transport decides which era a request belongs to and passes it down here; this
module only cares about producing the right response shape for that era.
"""

import json
import time

from . import SERVER_NAME, SUPPORTED_PROTOCOLS, PROTOCOL_MODERN, __version__
from . import permissions
from .panel_client import PanelApiError
from .registry import ToolContext, ToolError, validate

META_PROTOCOL = 'io.modelcontextprotocol/protocolVersion'
META_CLIENT_INFO = 'io.modelcontextprotocol/clientInfo'
META_CLIENT_CAPS = 'io.modelcontextprotocol/clientCapabilities'
META_SERVER_INFO = 'io.modelcontextprotocol/serverInfo'
META_SUBSCRIPTION_ID = 'io.modelcontextprotocol/subscriptionId'

ERA_MODERN = 'modern'
ERA_LEGACY = 'legacy'

# MCP-defined codes (spec 2026-07-28, basic/index#error-codes).
CODE_HEADER_MISMATCH = -32020
CODE_MISSING_CAPABILITY = -32021
CODE_UNSUPPORTED_VERSION = -32022
CODE_INVALID_PARAMS = -32602
CODE_METHOD_NOT_FOUND = -32601
CODE_INTERNAL = -32603
CODE_PARSE = -32700
CODE_INVALID_REQUEST = -32600

INSTRUCTIONS = """\
This server controls a live aaPanel hosting server: websites, TLS certificates, \
databases, mail, FTP, cron jobs, files, the firewall, Docker and installed apps.

Working rules:
- Changes take effect immediately on a production machine. Read before you write: \
list or inspect the thing you are about to change, and tell the user what you found.
- Tools are grouped into tiers (read, write, destructive, shell, raw). Only the tiers \
the administrator enabled are visible. If a capability you need is missing, say so and \
name the tier instead of trying to reach it another way.
- Destructive and shell tools refuse their first call and return a confirm token. Show \
the user exactly what will happen, and only repeat the call with confirm=<token> once \
they agree. Never invent a token.
- Prefer the specific tool over panel_raw_call. The raw tools exist for panel features \
that have no dedicated tool yet.
- Every call is written to an audit log the administrator can read.\
"""


class ProtocolError(Exception):
    """A JSON-RPC level failure: the request itself is wrong."""

    def __init__(self, code, message, data=None, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.http_status = http_status

    def to_response(self, request_id=None):
        error = {'code': self.code, 'message': self.message}
        if self.data is not None:
            error['data'] = self.data
        response = {'jsonrpc': '2.0', 'error': error}
        if request_id is not None:
            response['id'] = request_id
        return response


def unsupported_version(requested):
    return ProtocolError(
        CODE_UNSUPPORTED_VERSION, 'Unsupported protocol version',
        {'supported': list(SUPPORTED_PROTOCOLS), 'requested': requested})


def header_mismatch(message):
    return ProtocolError(CODE_HEADER_MISMATCH, 'Header mismatch: %s' % message)


class RequestContext:
    def __init__(self, era=ERA_MODERN, protocol_version=PROTOCOL_MODERN, client_info=None,
                 client_capabilities=None, peer='', progress=None):
        self.era = era
        self.protocol_version = protocol_version
        self.client_info = client_info or {}
        self.client_capabilities = client_capabilities or {}
        self.peer = peer
        self.progress = progress

    @property
    def client_label(self):
        name = self.client_info.get('name') or 'unknown'
        version = self.client_info.get('version')
        return '%s/%s' % (name, version) if version else name


def meta_of(message):
    params = message.get('params')
    if isinstance(params, dict) and isinstance(params.get('_meta'), dict):
        return params['_meta']
    return {}


class McpServer:
    """Method dispatch shared by both transports."""

    def __init__(self, config_provider, registry, panel, audit=None, tool_loader=None):
        # A callable so config edits made in the panel UI take effect without a restart.
        self._config_provider = config_provider
        self.registry = registry
        self.panel = panel
        self.audit = audit
        self._tool_loader = tool_loader

    @property
    def config(self):
        return self._config_provider()

    # ------------------------------------------------------------------ identity

    def server_info(self):
        return {'name': SERVER_NAME, 'version': __version__}

    def capabilities(self):
        return {'tools': {'listChanged': True}, 'logging': {}}

    def visible_tools(self, config=None):
        return permissions.visible_tools(config or self.config, self.registry, self.panel)

    def tools_signature(self, config=None):
        return tuple(tool.name for tool in self.visible_tools(config))

    # ------------------------------------------------------------------ dispatch

    def dispatch(self, message, ctx):
        """Handle one JSON-RPC message. Returns a response dict, or None for a notification."""
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            raise ProtocolError(CODE_INVALID_REQUEST, 'Not a JSON-RPC 2.0 message')

        method = message.get('method')
        if not isinstance(method, str):
            raise ProtocolError(CODE_INVALID_REQUEST, 'Missing "method"')

        request_id = message.get('id')
        params = message.get('params')
        if params is not None and not isinstance(params, dict):
            raise ProtocolError(CODE_INVALID_PARAMS, '"params" must be an object')
        params = params or {}

        if request_id is None:
            self._handle_notification(method, params, ctx)
            return None

        handler = {
            'server/discover': self._discover,
            'initialize': self._initialize,
            'ping': lambda p, c: {},
            'tools/list': self._tools_list,
            'tools/call': self._tools_call,
            'logging/setLevel': self._set_level,
        }.get(method)

        if handler is None:
            raise ProtocolError(CODE_METHOD_NOT_FOUND, 'Method not found: %s' % method,
                                http_status=404)

        result = handler(params, ctx)
        if ctx.era == ERA_MODERN and isinstance(result, dict) and 'resultType' not in result:
            result = dict(result)
            result['resultType'] = 'complete'
        return {'jsonrpc': '2.0', 'id': request_id, 'result': result}

    def _handle_notification(self, method, params, ctx):
        # `notifications/initialized` and `notifications/cancelled` need no reply, and
        # nothing else is expected from a client. Unknown ones are ignored on purpose:
        # a notification has no id to answer an error on.
        return None

    # ------------------------------------------------------------------- methods

    def _discover(self, params, ctx):
        return {
            'supportedVersions': list(SUPPORTED_PROTOCOLS),
            'capabilities': self.capabilities(),
            'instructions': INSTRUCTIONS,
            '_meta': {META_SERVER_INFO: self.server_info()},
        }

    def _initialize(self, params, ctx):
        """Legacy handshake. Echo a version both sides know."""
        requested = params.get('protocolVersion') or ''
        if requested in SUPPORTED_PROTOCOLS:
            negotiated = requested
        elif requested and requested < PROTOCOL_MODERN:
            # Unknown but older: answer with our newest legacy revision rather than
            # failing, which is what the legacy lifecycle asks servers to do.
            negotiated = SUPPORTED_PROTOCOLS[1]
        else:
            raise unsupported_version(requested)
        ctx.protocol_version = negotiated
        return {
            'protocolVersion': negotiated,
            'capabilities': self.capabilities(),
            'serverInfo': self.server_info(),
            'instructions': INSTRUCTIONS,
        }

    def _set_level(self, params, ctx):
        return {}

    def _tools_list(self, params, ctx):
        if self._tool_loader:
            self._tool_loader(self.registry, self.panel)
        config = self.config
        return {'tools': [self._definition(tool, config) for tool in self.visible_tools(config)]}

    @staticmethod
    def _definition(tool, config):
        """The tool definition, with the confirm parameter advertised where it applies.

        A model cannot pass back a confirm token it was never told about, so tools in a
        confirm tier grow the parameter in their published schema.
        """
        definition = tool.definition()
        if not permissions.needs_confirmation(config, tool):
            return definition
        schema = json.loads(json.dumps(definition['inputSchema']))
        schema.setdefault('type', 'object')
        schema.setdefault('properties', {})[permissions.CONFIRM_FIELD] = {
            'type': 'string',
            'description': 'Confirmation token. Call once without it to receive the token '
                           'and a summary, show that to the user, then call again with the '
                           'token and identical arguments.',
        }
        definition['inputSchema'] = schema
        return definition

    def _tools_call(self, params, ctx):
        name = params.get('name')
        if not isinstance(name, str) or not name:
            raise ProtocolError(CODE_INVALID_PARAMS, '"name" is required')
        arguments = params.get('arguments')
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProtocolError(CODE_INVALID_PARAMS, '"arguments" must be an object')

        tool = self.registry.get(name)
        if tool is None or not tool.is_available(self.panel):
            raise ProtocolError(CODE_INVALID_PARAMS, 'Unknown tool: %s' % name)

        config = self.config
        started = time.time()
        outcome, detail = 'ok', ''
        try:
            permissions.check(config, tool, arguments)
            # `confirm` is a protocol-level argument, not a tool parameter, so it is
            # stripped before the tool's own schema sees it.
            call_args = {k: v for k, v in arguments.items() if k != permissions.CONFIRM_FIELD}
            validated = validate(tool.input_schema, call_args)
            context = ToolContext(self.panel, config, self.registry, self.audit,
                                  ctx.client_info, ctx.progress)
            payload = tool.handler(context, validated)
            return self._success(payload)
        except permissions.ConfirmationRequired as exc:
            outcome, detail = 'needs_confirmation', exc.message
            return self._failure(exc.message, exc.details)
        except permissions.PermissionDenied as exc:
            outcome, detail = 'denied', exc.message
            return self._failure(exc.message, exc.details)
        except ToolError as exc:
            outcome, detail = 'error', exc.message
            return self._failure(exc.message, exc.details)
        except PanelApiError as exc:
            outcome, detail = 'panel_error', exc.message
            message = exc.message
            if exc.remediation:
                message += '\n\nWhat to do: ' + exc.remediation
            return self._failure(message, exc.to_dict())
        except Exception as exc:  # a bug in a handler must not kill the daemon
            outcome, detail = 'exception', '%s: %s' % (type(exc).__name__, exc)
            return self._failure('The tool failed unexpectedly: %s' % detail)
        finally:
            if self.audit:
                self.audit.record(
                    tool=name,
                    tier=tool.tier,
                    arguments=arguments,
                    outcome=outcome,
                    detail=detail[:500],
                    client=ctx.client_label,
                    peer=ctx.peer,
                    duration_ms=int((time.time() - started) * 1000),
                )

    # -------------------------------------------------------------- result shapes

    @staticmethod
    def _success(payload):
        if payload is None:
            payload = {'status': 'ok'}
        if isinstance(payload, str):
            return {'content': [{'type': 'text', 'text': payload}], 'isError': False}
        text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        result = {'content': [{'type': 'text', 'text': text}], 'isError': False}
        if isinstance(payload, dict):
            result['structuredContent'] = payload
        return result

    @staticmethod
    def _failure(message, details=None):
        text = message
        if details:
            extra = json.dumps(details, indent=2, ensure_ascii=False, default=str)
            text = '%s\n\n%s' % (message, extra)
        return {'content': [{'type': 'text', 'text': text}], 'isError': True}


def subscription_filter(params):
    """The subset of a subscriptions/listen filter this server honours."""
    requested = params.get('notifications') or {}
    agreed = {}
    if requested.get('toolsListChanged'):
        agreed['toolsListChanged'] = True
    return agreed


def notification(method, params=None, subscription_id=None):
    message = {'jsonrpc': '2.0', 'method': method}
    payload = dict(params or {})
    if subscription_id is not None:
        payload.setdefault('_meta', {})[META_SUBSCRIPTION_ID] = subscription_id
    if payload:
        message['params'] = payload
    return message
