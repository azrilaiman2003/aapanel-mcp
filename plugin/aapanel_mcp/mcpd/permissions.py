# coding: utf-8
"""Which tools an agent may see, and which ones need a second look first.

Two independent gates:

1. **Tiers.** Every tool declares one of read/write/destructive/shell/raw. A fresh
   install enables only `read`. A disabled tool is absent from tools/list, not merely
   refused when called — an agent cannot ask for a capability it never saw.

2. **Confirmation.** Tools in the confirm tiers refuse their first call and hand back
   a summary of what they are about to do plus a short-lived token. The agent repeats
   the call with `confirm: "<token>"`. The token is an HMAC over the tool name and the
   exact arguments, so it is not transferable to a different call and no server-side
   state is needed — which matters, because the modern MCP transport is stateless and
   has no session to park a pending confirmation in.
"""

import hashlib
import hmac
import json
import time

from .registry import TIER_DESTRUCTIVE, TIER_SHELL, ToolError

CONFIRM_FIELD = 'confirm'


class PermissionDenied(ToolError):
    pass


class ConfirmationRequired(ToolError):
    def __init__(self, message, token, summary):
        super().__init__(message, {'confirm_token': token, 'summary': summary})
        self.token = token
        self.summary = summary


def tier_enabled(config, tier):
    return bool(config['permissions']['tiers'].get(tier, False))


def tool_enabled(config, tool):
    """Per-tool override wins over the tier default, in both directions."""
    override = config['permissions']['tools'].get(tool.name)
    if override is not None:
        return bool(override)
    return tier_enabled(config, tool.tier)


def visible_tools(config, registry, panel=None):
    tools = []
    for tool in registry.all():
        if not tool_enabled(config, tool):
            continue
        if panel is not None and not tool.is_available(panel):
            continue
        tools.append(tool)
    return tools


def require_enabled(config, tool):
    if tool_enabled(config, tool):
        return
    raise PermissionDenied(
        'The tool "%s" is disabled on this server. It belongs to the "%s" tier, which the '
        'administrator has not enabled. Ask them to turn it on in aaPanel -> AI MCP Server '
        '-> Permissions.' % (tool.name, tool.tier),
        {'tool': tool.name, 'tier': tool.tier})


# ------------------------------------------------------------------ confirm tokens

def needs_confirmation(config, tool):
    confirm = config.get('confirm') or {}
    if not confirm.get('required', True):
        return False
    tiers = confirm.get('tiers') or [TIER_DESTRUCTIVE, TIER_SHELL]
    return tool.tier in tiers


def _canonical(arguments):
    payload = {k: v for k, v in (arguments or {}).items() if k != CONFIRM_FIELD}
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)


def _sign(secret, tool_name, arguments, expiry):
    message = '%d|%s|%s' % (expiry, tool_name, _canonical(arguments))
    return hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()[:32]


def issue_token(config, tool_name, arguments, now=None):
    now = int(now if now is not None else time.time())
    ttl = int(config['confirm'].get('ttl_seconds') or 300)
    expiry = now + ttl
    return '%d.%s' % (expiry, _sign(config['confirm']['secret'], tool_name, arguments, expiry))


def verify_token(config, tool_name, arguments, token, now=None):
    now = int(now if now is not None else time.time())
    if not token or not isinstance(token, str) or '.' not in token:
        return False
    expiry_text, _, signature = token.partition('.')
    try:
        expiry = int(expiry_text)
    except ValueError:
        return False
    if expiry < now:
        return False
    expected = _sign(config['confirm']['secret'], tool_name, arguments, expiry)
    return hmac.compare_digest(expected, signature)


def describe_effect(tool, arguments):
    """One line the human will actually read before approving."""
    detail = ', '.join('%s=%s' % (key, value)
                       for key, value in sorted((arguments or {}).items())
                       if key != CONFIRM_FIELD and not _is_secretish(key))
    return '%s (%s)%s' % (tool.title, tool.name, ': ' + detail if detail else '')


def _is_secretish(key):
    lowered = key.lower()
    return any(word in lowered for word in ('password', 'passwd', 'token', 'secret', 'key'))


def check(config, tool, arguments):
    """Run both gates. Returns None, or raises ToolError describing what to do next."""
    require_enabled(config, tool)
    if not needs_confirmation(config, tool):
        return
    supplied = (arguments or {}).get(CONFIRM_FIELD)
    if verify_token(config, tool.name, arguments, supplied):
        return
    token = issue_token(config, tool.name, arguments)
    summary = describe_effect(tool, arguments)
    if supplied:
        raise ConfirmationRequired(
            'That confirmation token is invalid or has expired. This call needs a fresh '
            'one. Show the operation to the user, then repeat the call with '
            'confirm="%s". Operation: %s' % (token, summary), token, summary)
    raise ConfirmationRequired(
        'This operation changes or removes things on the server, so it needs confirmation. '
        'Tell the user exactly what will happen, and if they agree repeat this call with '
        'confirm="%s" and identical arguments. Operation: %s' % (token, summary),
        token, summary)
