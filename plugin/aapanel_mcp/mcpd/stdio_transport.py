# coding: utf-8
"""stdio transport.

One JSON-RPC message per line on stdin, one per line on stdout. Nothing else may go
to stdout — diagnostics go to stderr, or they corrupt the stream.

Era selection follows the spec's rule for a dual-era stdio server: an `initialize`
request switches the whole process to legacy semantics, while requests carrying
per-request `_meta` are served as modern. The usual way to reach this entry point is
`ssh root@host /www/server/panel/plugin/aapanel_mcp/bin/aapanel-mcp-stdio`, which
gives a remote client a working MCP server without opening a port.
"""

import json
import sys

from . import PROTOCOL_MODERN, SUPPORTED_PROTOCOLS
from .protocol import (ERA_LEGACY, ERA_MODERN, ProtocolError, RequestContext,
                       META_CLIENT_CAPS, META_CLIENT_INFO, META_PROTOCOL,
                       CODE_PARSE, CODE_INVALID_REQUEST, meta_of, unsupported_version)


class StdioTransport:
    def __init__(self, mcp_server, stdin=None, stdout=None, stderr=None):
        self.server = mcp_server
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.era = None  # decided by the first request that says something about itself

    def log(self, message):
        print(message, file=self.stderr, flush=True)

    def _write(self, payload):
        self.stdout.write(json.dumps(payload) + '\n')
        self.stdout.flush()

    def _context_for(self, message):
        method = message.get('method') or ''
        meta = meta_of(message)

        if method == 'initialize':
            self.era = ERA_LEGACY
            version = (message.get('params') or {}).get('protocolVersion') or ''
            return RequestContext(ERA_LEGACY, version, peer='stdio')

        version = meta.get(META_PROTOCOL)
        if version:
            if version not in SUPPORTED_PROTOCOLS:
                raise unsupported_version(version)
            era = ERA_MODERN if version == PROTOCOL_MODERN else ERA_LEGACY
            self.era = era
            return RequestContext(era, version,
                                  client_info=meta.get(META_CLIENT_INFO),
                                  client_capabilities=meta.get(META_CLIENT_CAPS), peer='stdio')

        # Nothing declared: stay in whichever era this process already settled into.
        era = self.era or ERA_MODERN
        return RequestContext(era, PROTOCOL_MODERN if era == ERA_MODERN else SUPPORTED_PROTOCOLS[1],
                              peer='stdio')

    def handle_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            message = json.loads(line)
        except ValueError:
            self._write({'jsonrpc': '2.0', 'error': {'code': CODE_PARSE, 'message': 'Invalid JSON'}})
            return
        if not isinstance(message, dict):
            self._write({'jsonrpc': '2.0',
                         'error': {'code': CODE_INVALID_REQUEST,
                                   'message': 'Expected a single JSON-RPC message'}})
            return

        request_id = message.get('id')
        try:
            ctx = self._context_for(message)
            response = self.server.dispatch(message, ctx)
        except ProtocolError as exc:
            if request_id is not None:
                self._write(exc.to_response(request_id))
            return
        except Exception as exc:
            self.log('dispatch failed: %s' % exc)
            if request_id is not None:
                self._write({'jsonrpc': '2.0', 'id': request_id,
                             'error': {'code': -32603, 'message': 'Internal error: %s' % exc}})
            return
        if response is not None:
            self._write(response)

    def run(self):
        for line in self.stdin:
            self.handle_line(line)
        return 0
