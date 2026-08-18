#!/usr/bin/env python3
# coding: utf-8
"""End-to-end check: run the real daemon and probe it like a client would.

Starts a fake aaPanel, launches aapanel_mcp_service as its own process against it,
then runs scripts/mcp_probe.py over both HTTP and stdio. This is the closest thing to
a live install that works on a machine with no aaPanel on it.

    python3 tests/live_check.py
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import FakePanel, basic_routes, PLUGIN_SRC, REPO_ROOT

TOKEN = 'live-check-token'
PORT = 47801


def wait_for_health(url, attempts=60):
    import urllib.error
    import urllib.request
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.getcode() == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


def main():
    panel = FakePanel()
    basic_routes(panel)
    panel.route('/site', 'GetSiteDomains', {'domains': [{'name': 'example.com', 'id': 1}]})

    from mcpd import config as cfg
    config = cfg.load()
    config['bind'].update({'mode': 'bound', 'host': '127.0.0.1', 'port': PORT, 'path': '/mcp'})
    config['auth']['token'] = TOKEN
    cfg.save(config)

    env = dict(os.environ)
    env['AAPANEL_PANEL_HOME'] = panel.panel_home
    env['AAPANEL_MCP_HOME'] = panel.plugin_home

    service = os.path.join(PLUGIN_SRC, 'aapanel_mcp_service')
    probe = os.path.join(REPO_ROOT, 'scripts', 'mcp_probe.py')
    stdio = os.path.join(PLUGIN_SRC, 'bin', 'aapanel-mcp-stdio')

    print('== --check ==')
    subprocess.run([sys.executable, service, '--check'], env=env)

    print('\n== starting the daemon ==')
    daemon = subprocess.Popen([sys.executable, service], env=env)
    failures = 0
    try:
        if not wait_for_health('http://127.0.0.1:%d/healthz' % PORT):
            print('the daemon never became healthy')
            print(open(os.path.join(panel.plugin_home, 'data', 'daemon.log')).read())
            return 1
        print('healthy\n')

        print('== probing over HTTP ==')
        result = subprocess.run([sys.executable, probe, 'http://127.0.0.1:%d/mcp' % PORT,
                                 '--token', TOKEN, '--call', 'site_list'], env=env)
        failures += result.returncode

        print('\n== probing over stdio ==')
        # Run it through this interpreter: the shipped shebang points at the panel's
        # virtualenv, which only exists on a machine that actually has aaPanel.
        result = subprocess.run([sys.executable, probe, '%s %s' % (sys.executable, stdio),
                                 '--stdio', '--call', 'site_list'], env=env)
        failures += result.returncode

        print('\n== rejecting a bad token ==')
        result = subprocess.run([sys.executable, probe, 'http://127.0.0.1:%d/mcp' % PORT,
                                 '--token', 'wrong'], env=env, capture_output=True, text=True)
        if result.returncode == 0:
            print('  FAIL   a wrong token was accepted')
            failures += 1
        else:
            print('  PASS   a wrong token is refused')

        audit = os.path.join(panel.plugin_home, 'data', 'audit.log')
        print('\n== audit log ==')
        if os.path.exists(audit):
            with open(audit, encoding='utf-8') as fp:
                for line in fp:
                    entry = json.loads(line)
                    print('  %s %-12s %-6s %s' % (entry.get('time'), entry.get('tool'),
                                                  entry.get('outcome'), entry.get('client')))
        else:
            print('  FAIL   nothing was audited')
            failures += 1
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
        panel.stop()

    print('\n%s' % ('FAILURES: %d' % failures if failures else 'live check passed'))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
