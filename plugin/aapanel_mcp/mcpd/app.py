# coding: utf-8
"""Assembly: turns config on disk into a running MCP server.

Both transports and the test suite go through here, so there is exactly one description
of how the pieces fit together.
"""

import os
import time

from . import config as cfg
from .audit import AuditLog
from .panel_client import PanelClient
from .protocol import McpServer
from .tools import build_registry

LEVELS = {'debug': 10, 'info': 20, 'warn': 30, 'error': 40}


class Logger:
    """Small line logger. Writes to a file for the daemon, to stderr for stdio."""

    def __init__(self, path=None, level='info', stream=None):
        self.path = path
        self.stream = stream
        self.level = LEVELS.get(level, 20)

    def __call__(self, kind, message):
        severity = LEVELS.get(kind, 20)
        if kind in ('deny', 'error'):
            severity = LEVELS['error']
        if severity < self.level and kind not in ('start', 'stop', 'deny', 'error'):
            return
        line = '%s [%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), kind, message)
        try:
            if self.path:
                with open(self.path, 'a', encoding='utf-8') as fp:
                    fp.write(line)
            elif self.stream:
                self.stream.write(line)
                self.stream.flush()
        except OSError:
            pass


class ConfigProvider:
    """Re-reads config.json when it changes, so the panel UI can retune a running daemon."""

    def __init__(self, reload_interval=2.0):
        self._config = cfg.load()
        self._checked = 0.0
        self._mtime = self._current_mtime()
        self.reload_interval = reload_interval

    @staticmethod
    def _current_mtime():
        try:
            return os.path.getmtime(cfg.config_path())
        except OSError:
            return 0.0

    def __call__(self):
        now = time.time()
        if now - self._checked >= self.reload_interval:
            self._checked = now
            mtime = self._current_mtime()
            if mtime != self._mtime:
                self._mtime = mtime
                self._config = cfg.load()
        return self._config


def build(logger=None):
    """Returns (config_provider, mcp_server)."""
    provider = ConfigProvider()
    config = provider()
    panel = PanelClient(timeout=config['limits'].get('panel_timeout'))
    registry = build_registry()
    audit = AuditLog(config)
    server = McpServer(provider, registry, panel, audit)
    if logger:
        logger('start', 'loaded %d tools' % len(registry))
    return provider, server

