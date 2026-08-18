#!/usr/bin/env python3
# coding: utf-8
"""Check a running aaPanel MCP server the way a real client would.

    python3 scripts/mcp_probe.py http://127.0.0.1:7801/mcp --token TOKEN
    python3 scripts/mcp_probe.py --stdio /www/server/panel/plugin/aapanel_mcp/bin/aapanel-mcp-stdio
    python3 scripts/mcp_probe.py URL --token TOKEN --call site_list

Runs the modern (2026-07-28) handshake-free flow, then repeats the version negotiation
as a legacy client, so both eras get exercised. Prints one line per check.
"""

import argparse
import json
import shlex
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

PROTOCOL = '2026-07-28'
LEGACY = '2025-06-18'
CLIENT = {'name': 'aapanel-mcp-probe', 'version': '1.0.0'}

PASS, FAIL = '  PASS  ', '  FAIL  '


class Result:
    def __init__(self):
        self.failures = 0

    def check(self, name, ok, detail=''):
        print('%s %-42s %s' % (PASS if ok else FAIL, name, detail))
        if not ok:
            self.failures += 1
        return ok


class HttpClient:
    def __init__(self, url, token, insecure=False):
        self.url = url
        self.token = token
        self.context = ssl._create_unverified_context() if insecure else None

    def send(self, message, headers=None, legacy=False):
        body = json.dumps(message).encode('utf-8')
        request = urllib.request.Request(self.url, data=body, method='POST')
        request.add_header('Content-Type', 'application/json')
        request.add_header('Accept', 'application/json, text/event-stream')
        if self.token:
            request.add_header('Authorization', 'Bearer %s' % self.token)
        if not legacy:
            request.add_header('MCP-Protocol-Version', PROTOCOL)
            request.add_header('Mcp-Method', message['method'])
            name = (message.get('params') or {}).get('name')
            if name:
                request.add_header('Mcp-Name', name)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            opener = (urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.context))
                      if self.context else urllib.request.build_opener())
            with opener.open(request, timeout=30) as response:
                raw = response.read().decode('utf-8')
                return response.getcode(), json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode('utf-8')
            try:
                return exc.code, json.loads(raw) if raw else None
            except ValueError:
                return exc.code, {'raw': raw[:200]}


class StdioClient:
    def __init__(self, command):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def send(self, message, headers=None, legacy=False):
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        return 200, (json.loads(line) if line.strip() else None)

    def close(self):
        try:
            self.process.stdin.close()
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()


def modern(method, params=None, request_id=1):
    params = dict(params or {})
    params['_meta'] = {
        'io.modelcontextprotocol/protocolVersion': PROTOCOL,
        'io.modelcontextprotocol/clientInfo': CLIENT,
        'io.modelcontextprotocol/clientCapabilities': {},
    }
    return {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('target', help='endpoint URL, or the stdio command line with --stdio')
    parser.add_argument('--token', default='', help='bearer token for the HTTP endpoint')
    parser.add_argument('--stdio', action='store_true', help='talk to a stdio server instead')
    parser.add_argument('--insecure', action='store_true', help='skip TLS verification')
    parser.add_argument('--call', default='', help='also call this read-only tool')
    args = parser.parse_args()

    client = (StdioClient(shlex.split(args.target)) if args.stdio
              else HttpClient(args.target, args.token, args.insecure))
    result = Result()

    status, response = client.send(modern('server/discover'))
    discovered = isinstance(response, dict) and 'result' in response
    result.check('server/discover answers', discovered,
                 '' if discovered else 'HTTP %s %s' % (status, json.dumps(response)[:120]))
    if not discovered:
        return 1

    info = response['result']
    result.check('advertises the current protocol', PROTOCOL in info.get('supportedVersions', []),
                 ', '.join(info.get('supportedVersions', [])))
    result.check('results carry resultType', info.get('resultType') == 'complete')
    server_info = (info.get('_meta') or {}).get('io.modelcontextprotocol/serverInfo') or {}
    result.check('identifies itself', bool(server_info.get('name')),
                 '%s %s' % (server_info.get('name', '?'), server_info.get('version', '')))
    result.check('has instructions for the model', bool(info.get('instructions')))

    status, response = client.send(modern('tools/list', request_id=2))
    tools = ((response or {}).get('result') or {}).get('tools') or []
    result.check('tools/list returns tools', bool(tools), '%d visible' % len(tools))
    if tools:
        shapes = all(isinstance(tool.get('inputSchema'), dict) and tool.get('description')
                     for tool in tools)
        result.check('every tool has a schema and a description', shapes)

    if not args.stdio:
        status, response = client.send(
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list',
             'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': '1999-01-01'}}},
            headers={'MCP-Protocol-Version': '1999-01-01', 'Mcp-Method': 'tools/list'},
            legacy=True)
        error = (response or {}).get('error') or {}
        result.check('rejects an unknown protocol version', error.get('code') == -32022,
                     'lists %s' % ', '.join((error.get('data') or {}).get('supported', [])))

    status, response = client.send(
        {'jsonrpc': '2.0', 'id': 4, 'method': 'initialize',
         'params': {'protocolVersion': LEGACY, 'capabilities': {}, 'clientInfo': CLIENT}},
        legacy=True)
    negotiated = ((response or {}).get('result') or {}).get('protocolVersion')
    result.check('legacy initialize still works', negotiated == LEGACY, negotiated or '')

    if args.call:
        status, response = client.send(modern('tools/call', {'name': args.call, 'arguments': {}},
                                              request_id=5))
        payload = (response or {}).get('result') or {}
        ok = payload and not payload.get('isError')
        text = (payload.get('content') or [{}])[0].get('text', '')
        result.check('tools/call %s' % args.call, bool(ok), text[:160].replace('\n', ' '))

    if args.stdio:
        client.close()

    print('\n%d check(s) failed' % result.failures if result.failures else '\nAll checks passed')
    return 1 if result.failures else 0


if __name__ == '__main__':
    sys.exit(main())
