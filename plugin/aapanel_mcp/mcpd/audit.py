# coding: utf-8
"""Append-only audit log of every tool call.

One JSON object per line, so the UI can tail it cheaply and an operator can grep it.
Secrets never reach the file: values are redacted both by field name (anything that
looks like a password or key) and by value, so the bearer token cannot leak in through
some field we did not anticipate.
"""

import json
import os
import threading
import time

from . import config as cfg

_LOCK = threading.Lock()

REDACTED = '***'
_SECRET_HINTS = ('password', 'passwd', 'pwd', 'token', 'secret', 'apikey', 'api_key',
                 'privatekey', 'private_key', 'ssh_key', 'sk')
MAX_VALUE_CHARS = 512


def _looks_secret(key):
    lowered = str(key).lower().replace('-', '_')
    return any(hint in lowered for hint in _SECRET_HINTS)


def redact(value, extra_secrets=(), _depth=0):
    """Recursively strip secrets and cap the size of anything we write down."""
    if _depth > 6:
        return '<nested>'
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            result[key] = REDACTED if _looks_secret(key) else redact(item, extra_secrets, _depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        capped = list(value)[:50]
        return [redact(item, extra_secrets, _depth + 1) for item in capped]
    if isinstance(value, str):
        for secret in extra_secrets:
            if secret and len(secret) >= 8 and secret in value:
                value = value.replace(secret, REDACTED)
        if len(value) > MAX_VALUE_CHARS:
            return value[:MAX_VALUE_CHARS] + '...<truncated>'
        return value
    return value


class AuditLog:
    def __init__(self, config, path=None):
        self.config = config
        self.path = path or cfg.audit_path()
        self.enabled = bool(config.get('audit', {}).get('enabled', True))
        self.max_bytes = int(config.get('audit', {}).get('max_bytes') or 5 * 1024 * 1024)
        self.keep = int(config.get('audit', {}).get('keep') or 3)
        self._secrets = [
            config.get('auth', {}).get('token') or '',
            config.get('confirm', {}).get('secret') or '',
        ]

    def record(self, **fields):
        if not self.enabled:
            return
        entry = {'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}
        entry.update(redact(fields, self._secrets))
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with _LOCK:
            try:
                self._rotate_if_needed()
                with open(self.path, 'a', encoding='utf-8') as fp:
                    fp.write(line + '\n')
                os.chmod(self.path, 0o600)
            except OSError:
                # Losing an audit line must never take the server down with it.
                pass

    def _rotate_if_needed(self):
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        for index in range(self.keep, 0, -1):
            source = '%s.%d' % (self.path, index)
            if index == self.keep and os.path.exists(source):
                os.remove(source)
                continue
            if os.path.exists(source):
                os.replace(source, '%s.%d' % (self.path, index + 1))
        os.replace(self.path, self.path + '.1')

    def tail(self, limit=200, contains=''):
        """Most recent entries first, optionally filtered by a substring match."""
        entries = []
        for path in [self.path, self.path + '.1']:
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding='utf-8', errors='replace') as fp:
                    lines = fp.readlines()
            except OSError:
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                if contains and contains.lower() not in line.lower():
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
                if len(entries) >= limit:
                    return entries
        return entries

    def clear(self):
        with _LOCK:
            for path in [self.path] + ['%s.%d' % (self.path, i) for i in range(1, self.keep + 2)]:
                try:
                    os.remove(path)
                except OSError:
                    pass
