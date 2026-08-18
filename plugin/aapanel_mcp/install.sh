#!/bin/bash
# =============================================================================
#  aaPanel MCP Server - install / uninstall
# =============================================================================
#  aaPanel runs `chmod -R 600` over the plugin directory immediately before
#  calling this script, so nothing arrives executable. Every mode bit this
#  plugin needs is set here.
#
#  Usage: bash install.sh {install|uninstall}
# =============================================================================

PATH=/www/server/panel/pyenv/bin:/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin:/usr/local/sbin:~/bin
export PATH

PLUGIN_NAME=aapanel_mcp
SERVICE_NAME=aapanel-mcp
PLUGIN_PATH=/www/server/panel/plugin/${PLUGIN_NAME}
SYSTEMD_UNIT=/etc/systemd/system/${SERVICE_NAME}.service
INIT_SCRIPT=/etc/init.d/${PLUGIN_NAME}

# -----------------------------------------------------------------------------
# find_python
#
# Prefer the panel's own interpreter so the daemon runs on the same Python the
# panel was tested with. Fall back to the system one, which is fine because this
# plugin has no third-party dependencies.
# -----------------------------------------------------------------------------
find_python() {
    if [ -x /www/server/panel/pyenv/bin/python3 ]; then
        echo /www/server/panel/pyenv/bin/python3
    elif [ -x /usr/bin/python3 ]; then
        echo /usr/bin/python3
    else
        command -v python3
    fi
}

PYTHON_BIN=$(find_python)

# -----------------------------------------------------------------------------
# fix_modes
#
# Restores the modes aaPanel's importer flattened: executables for the two entry
# points and the shell scripts, 0700 for the data directory (it holds the access
# token and the audit log), 0644 for everything else.
# -----------------------------------------------------------------------------
fix_modes() {
    chmod -R 644 "${PLUGIN_PATH}" 2>/dev/null
    find "${PLUGIN_PATH}" -type d -exec chmod 755 {} \; 2>/dev/null
    chmod 755 "${PLUGIN_PATH}/install.sh" \
              "${PLUGIN_PATH}/upgrade.sh" \
              "${PLUGIN_PATH}/repair.sh" \
              "${PLUGIN_PATH}/aapanel_mcp_service" \
              "${PLUGIN_PATH}/bin/aapanel-mcp-stdio" 2>/dev/null
    mkdir -p "${PLUGIN_PATH}/data"
    chmod 700 "${PLUGIN_PATH}/data"
    chmod 600 "${PLUGIN_PATH}/data/"* 2>/dev/null
}

# -----------------------------------------------------------------------------
# fix_shebangs
#
# The shipped shebang points at the panel virtualenv. Rewrite it when that
# interpreter is not the one we resolved, so the scripts also work when run
# directly (an MCP client over SSH runs bin/aapanel-mcp-stdio by path).
# -----------------------------------------------------------------------------
fix_shebangs() {
    for file in "${PLUGIN_PATH}/aapanel_mcp_service" "${PLUGIN_PATH}/bin/aapanel-mcp-stdio"; do
        [ -f "$file" ] || continue
        sed -i "1s|^#!.*|#!${PYTHON_BIN}|" "$file"
    done
}

# -----------------------------------------------------------------------------
# seed_config
#
# Generates data/config.json with a fresh bearer token and confirm secret. The
# defaults are deliberately closed: loopback only, read-only tier.
# -----------------------------------------------------------------------------
seed_config() {
    "${PYTHON_BIN}" - <<PYEOF
import sys
sys.path.insert(0, '${PLUGIN_PATH}')
from mcpd import config as cfg
config = cfg.load()
print('token generated' if config['auth']['token'] else 'token missing')
PYEOF
    chmod 600 "${PLUGIN_PATH}/data/config.json" 2>/dev/null
}

install_service() {
    if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
        cat > "${SYSTEMD_UNIT}" <<UNITEOF
[Unit]
Description=aaPanel MCP Server
Documentation=https://github.com/azrilaiman2003/aapanel-mcp
After=network.target

[Service]
Type=simple
WorkingDirectory=${PLUGIN_PATH}
ExecStart=${PYTHON_BIN} ${PLUGIN_PATH}/aapanel_mcp_service
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
UNITEOF
        chmod 644 "${SYSTEMD_UNIT}"
        systemctl daemon-reload
        systemctl enable ${SERVICE_NAME} >/dev/null 2>&1
        echo "Installed systemd unit ${SYSTEMD_UNIT}"
    fi

    # Always write the SysV script too: it is the fallback the panel plugin uses
    # when systemd is absent, and it is handy for manual starts either way.
    cat > "${INIT_SCRIPT}" <<INITEOF
#!/bin/bash
# chkconfig: 2345 60 20
### BEGIN INIT INFO
# Provides:          ${PLUGIN_NAME}
# Required-Start:    \$all
# Required-Stop:     \$all
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: aaPanel MCP Server
### END INIT INFO

PLUGIN_PATH=${PLUGIN_PATH}
PYTHON_BIN=${PYTHON_BIN}
PID_FILE=\${PLUGIN_PATH}/data/daemon.pid

start() {
    if [ -f "\${PID_FILE}" ] && [ -d "/proc/\$(cat \${PID_FILE})" ]; then
        echo "already running"; return 0
    fi
    nohup \${PYTHON_BIN} \${PLUGIN_PATH}/aapanel_mcp_service >> \${PLUGIN_PATH}/data/daemon.log 2>&1 &
    sleep 1
    echo "started"
}

stop() {
    if [ -f "\${PID_FILE}" ]; then
        kill "\$(cat \${PID_FILE})" 2>/dev/null
        sleep 1
        [ -d "/proc/\$(cat \${PID_FILE} 2>/dev/null)" ] && kill -9 "\$(cat \${PID_FILE})" 2>/dev/null
        rm -f "\${PID_FILE}"
    fi
    echo "stopped"
}

case "\$1" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    status)
        if [ -f "\${PID_FILE}" ] && [ -d "/proc/\$(cat \${PID_FILE})" ]; then
            echo "running (\$(cat \${PID_FILE}))"
        else
            echo "stopped"
        fi ;;
    *) echo "usage: \$0 {start|stop|restart|status}"; exit 1 ;;
esac
INITEOF
    chmod 755 "${INIT_SCRIPT}"
}

start_service() {
    if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1 && [ -f "${SYSTEMD_UNIT}" ]; then
        systemctl restart ${SERVICE_NAME}
    else
        ${INIT_SCRIPT} restart
    fi
    sleep 1
}

Install() {
    echo "Installing the aaPanel MCP Server..."
    fix_modes
    fix_shebangs
    seed_config
    install_service
    start_service

    if [ -f "${PLUGIN_PATH}/data/daemon.pid" ]; then
        echo "MCP server running on 127.0.0.1 with the read-only tier enabled."
    else
        echo "The service was installed but is not running yet; open the plugin and press Start."
    fi
    echo "Open App Store -> AI MCP Server to enable the panel API and get the connection details."
    echo "Install OK"
}

Uninstall() {
    echo "Removing the aaPanel MCP Server..."
    if command -v systemctl >/dev/null 2>&1 && [ -f "${SYSTEMD_UNIT}" ]; then
        systemctl stop ${SERVICE_NAME} >/dev/null 2>&1
        systemctl disable ${SERVICE_NAME} >/dev/null 2>&1
        rm -f "${SYSTEMD_UNIT}"
        systemctl daemon-reload
    fi
    [ -f "${INIT_SCRIPT}" ] && ${INIT_SCRIPT} stop >/dev/null 2>&1
    rm -f "${INIT_SCRIPT}"
    rm -rf "${PLUGIN_PATH}"
    rm -f /www/server/panel/BTPanel/static/img/soft_ico/ico-${PLUGIN_NAME}.png
    echo "Uninstall OK. The panel API setting was left as it is; turn it off under Settings -> API if nothing else uses it."
}

if [ "${1}" == 'install' ]; then
    Install
elif [ "${1}" == 'uninstall' ]; then
    Uninstall
else
    echo 'Usage: bash install.sh {install|uninstall}'
    exit 1
fi
