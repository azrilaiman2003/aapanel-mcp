# coding: utf-8
"""aaPanel MCP server internals.

Standard library only, on purpose: this runs inside aaPanel's own Python
environment on machines that are often firewalled off from PyPI, and installing
packages into the panel's pyenv is a good way to break the panel.
"""

__version__ = '1.0.0'

# The MCP protocol revisions this server speaks, newest first. 2026-07-28 is the
# stateless "modern" era (per-request _meta, mandatory server/discover); the rest
# are "legacy" and negotiated through an initialize handshake.
PROTOCOL_MODERN = '2026-07-28'
PROTOCOL_LEGACY = ('2025-11-25', '2025-06-18', '2025-03-26')
SUPPORTED_PROTOCOLS = (PROTOCOL_MODERN,) + PROTOCOL_LEGACY

SERVER_NAME = 'aapanel-mcp'
