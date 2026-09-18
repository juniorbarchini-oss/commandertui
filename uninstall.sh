#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[!] needs root - re-running with sudo"
    exec sudo "$0" "$@"
fi

echo "--> removing /opt/commandertui"
rm -rf /opt/commandertui
echo "--> removing /usr/local/bin/commandertui"
rm -f /usr/local/bin/commandertui

if [ "${1:-}" = "--purge" ]; then
    echo "--> removing ~/.config/commandertui (bookmarks)"
    rm -rf "${SUDO_USER:+/home/$SUDO_USER}/.config/commandertui" 2>/dev/null || true
fi

echo "=== Done ==="
