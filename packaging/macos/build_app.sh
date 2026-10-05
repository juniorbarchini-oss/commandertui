#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${SCRIPT_DIR}/CommanderTUI.app"

echo "==> Building CommanderTUI.app..."
rm -rf "${APP_DIR}"
mkdir -p "${APP_DIR}/Contents/MacOS"
mkdir -p "${APP_DIR}/Contents/Resources"

cp "${SCRIPT_DIR}/Info.plist" "${APP_DIR}/Contents/Info.plist"
cp "${SCRIPT_DIR}/CommanderTUI" "${APP_DIR}/Contents/MacOS/CommanderTUI"
chmod +x "${APP_DIR}/Contents/MacOS/CommanderTUI"

if [ -f "${SCRIPT_DIR}/commandertui.icns" ]; then
    cp "${SCRIPT_DIR}/commandertui.icns" "${APP_DIR}/Contents/Resources/commandertui.icns"
fi

echo "==> App bundle built at: ${APP_DIR}"

if [[ "${1:-}" == "--install" ]]; then
    echo "==> Installing to /Applications/CommanderTUI.app..."
    rm -rf "/Applications/CommanderTUI.app"
    cp -R "${APP_DIR}" "/Applications/CommanderTUI.app"
    echo "==> Installed successfully to /Applications/CommanderTUI.app"
fi
