#!/usr/bin/env bash
# Removes SCopy and its settings. Your recordings and the downloaded source folder are left in place.
set -euo pipefail

INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/scopy"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/scopy"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/scopy.service"

systemctl --user disable --now scopy.service 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload
rm -rf "$INSTALL_DIR" "$CONFIG_DIR"

echo "SCopy removed."
