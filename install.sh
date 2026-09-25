#!/usr/bin/env bash
# ==============================================================================
# System installer for CommanderTUI.
# Deploys to /opt/commandertui with a global `commandertui` command in
# /usr/local/bin, and builds a self-contained Python venv there so the app
# never depends on system-wide site-packages.
# ==============================================================================
set -euo pipefail

APP_NAME="commandertui"
INSTALL_DIR="/opt/${APP_NAME}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FORCE=0
for a in "$@"; do case "$a" in --upgrade|--force) FORCE=1 ;; *) echo "unknown option: $a"; exit 2 ;; esac; done

if [ "$(id -u)" -ne 0 ]; then
    echo "[!] needs root - re-running with sudo"
    exec sudo "$0" "$@"
fi

echo "=== Installing ${APP_NAME} ==="

echo "--> ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
rm -rf "${INSTALL_DIR:?}/commandertui" "${INSTALL_DIR:?}/bin" "${INSTALL_DIR:?}/main.py"
cp -r "${SRC_DIR}/commandertui" "${SRC_DIR}/bin" "${SRC_DIR}/main.py" \
      "${SRC_DIR}/requirements.txt" "${INSTALL_DIR}/"
chmod +x "${INSTALL_DIR}/main.py" "${INSTALL_DIR}/bin/"*

VENV_DIR="${INSTALL_DIR}/.venv"
if [ "${FORCE}" -eq 1 ] && [ -d "${VENV_DIR}" ]; then
    echo "--> rebuilding ${VENV_DIR}"
    rm -rf "${VENV_DIR}"
fi
if [ ! -d "${VENV_DIR}" ]; then
    if python3 -c 'import venv' 2>/dev/null; then
        echo "--> building venv at ${VENV_DIR}"
        python3 -m venv "${VENV_DIR}"
        "${VENV_DIR}/bin/pip" install --upgrade pip -q
        "${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q
    else
        echo "[!] python3 venv module unavailable - falling back to system python3"
    fi
fi

echo "--> /usr/local/bin/commandertui"
ln -sf "${INSTALL_DIR}/bin/commandertui" /usr/local/bin/commandertui
echo "--> /usr/local/bin/commandertui-window"
ln -sf "${INSTALL_DIR}/bin/commandertui-window" /usr/local/bin/commandertui-window

echo "=== Done ==="
echo "  commandertui              open in \$HOME on both panes"
echo "  commandertui DIR1 DIR2    open comparing two folders"
echo "  commandertui-window       same, in its own floating window"
echo "  sudo ./uninstall.sh       remove it all"
