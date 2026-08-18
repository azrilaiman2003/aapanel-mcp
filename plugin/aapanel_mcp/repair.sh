#!/bin/bash
# Diagnose a plugin that is installed but not working.
PLUGIN_PATH=/www/server/panel/plugin/aapanel_mcp
PYTHON_BIN=/www/server/panel/pyenv/bin/python3
[ -x "${PYTHON_BIN}" ] || PYTHON_BIN=/usr/bin/python3

echo "== files =="
for f in info.json install.sh index.html aapanel_mcp_main.py aapanel_mcp_service \
         bin/aapanel-mcp-stdio mcpd/protocol.py mcpd/tools/__init__.py; do
    if [ -s "${PLUGIN_PATH}/${f}" ]; then echo "  ok      ${f}"; else echo "  MISSING ${f}"; fi
done

echo "== python =="
echo "  interpreter: ${PYTHON_BIN} ($(${PYTHON_BIN} -V 2>&1))"
rm -rf "${PLUGIN_PATH}/__pycache__" "${PLUGIN_PATH}/mcpd/__pycache__" \
       "${PLUGIN_PATH}/mcpd/tools/__pycache__" 2>/dev/null

echo "== self check =="
"${PYTHON_BIN}" "${PLUGIN_PATH}/aapanel_mcp_service" --check

echo "== service =="
if command -v systemctl >/dev/null 2>&1; then
    systemctl is-active aapanel-mcp 2>/dev/null || echo "  systemd unit not active"
fi
if [ -f "${PLUGIN_PATH}/data/daemon.pid" ]; then
    echo "  pid file: $(cat "${PLUGIN_PATH}/data/daemon.pid")"
fi

echo "== last log lines =="
tail -n 20 "${PLUGIN_PATH}/data/daemon.log" 2>/dev/null || echo "  no log yet"
