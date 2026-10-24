#!/usr/bin/env bash
# Installs (or updates) SCopy for the current user. No root needed.
# Start and stop sharing from the "SCopy" tab of the SteamVR dashboard once installed.
set -euo pipefail

PYOPENVR_VERSION="2.12.1401"
OPENVR_TAG="v2.12.14"   # matches the pyopenvr version above

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/framecopy"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/framecopy"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
VENV="$INSTALL_DIR/venv"

case "$(uname -m)" in
    aarch64) OPENVR_ARCH="linuxarm64" ;;
    x86_64)  OPENVR_ARCH="linux64" ;;
    *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

echo "==> Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$UNIT_DIR"
install -m 755 "$SRC_DIR/framecopy.py" "$INSTALL_DIR/framecopy.py"
install -m 644 "$SRC_DIR/steam_recordings.py" "$INSTALL_DIR/steam_recordings.py"
install -m 755 "$SRC_DIR/framecopy_app.py" "$INSTALL_DIR/framecopy_app.py"
install -m 755 "$SRC_DIR/uninstall.sh" "$INSTALL_DIR/uninstall.sh"
# Lets the tab's Update button find this checkout.
echo "$SRC_DIR" > "$CONFIG_DIR/source_dir"

echo "==> Setting up Python environment"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "openvr==$PYOPENVR_VERSION" "pillow>=10.1"

# pyopenvr only bundles an x86_64 loader library; swap in Valve's own build for this CPU.
echo "==> Fetching Valve's OpenVR loader ($OPENVR_ARCH)"
PKG_DIR="$("$VENV/bin/python" -c 'import importlib.util, os; print(os.path.dirname(importlib.util.find_spec("openvr").origin))')"
curl -fsSL -o "$PKG_DIR/libopenvr_api_64.so" \
    "https://raw.githubusercontent.com/ValveSoftware/openvr/$OPENVR_TAG/bin/$OPENVR_ARCH/libopenvr_api.so"

echo "==> Installing background service"
cat > "$UNIT_DIR/framecopy.service" <<UNIT
[Unit]
Description=SCopy (share recordings with browsers on your network)

[Service]
ExecStart=$VENV/bin/python $INSTALL_DIR/framecopy_app.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable framecopy.service
systemctl --user restart framecopy.service

echo
echo "Done. Put on the headset, open the SteamVR dashboard and pick the 'SCopy' tab"
echo "to start or stop sharing and see the address and PIN."
