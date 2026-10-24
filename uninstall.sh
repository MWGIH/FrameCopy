#!/usr/bin/env bash
# Removes SCopy and its settings. Your recordings and the downloaded source folder are left in place.
set -euo pipefail

INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/framecopy"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/framecopy"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/framecopy.service"

systemctl --user disable --now framecopy.service 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload
rm -rf "$INSTALL_DIR" "$CONFIG_DIR"

echo "SCopy removed."
