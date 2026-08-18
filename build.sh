#!/bin/bash
# Build dist/aapanel_mcp-<version>.zip for import into aaPanel.
set -e
cd "$(dirname "$0")"
exec python3 scripts/build_package.py "$@"
