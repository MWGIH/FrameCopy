#!/usr/bin/env bash
# Builds the Decky Loader plugin: decky/out/SCopy.zip, ready for Decky's "Install plugin from ZIP".
# Needs Node.js and pnpm (run it on a PC, or let GitHub Actions build it).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$HERE"

pnpm install --frozen-lockfile
pnpm run build

STAGE="$HERE/out/SCopy"
rm -rf "$HERE/out"
mkdir -p "$STAGE/dist" "$STAGE/py_modules" "$STAGE/assets"
cp dist/index.js "$STAGE/dist/"
cp main.py plugin.json package.json "$STAGE/"
cp "$ROOT/LICENSE" "$ROOT/README.md" "$STAGE/"
cp "$ROOT/assets/icon.png" "$STAGE/assets/"
# The web page itself is shared with the SteamVR app.
cp "$ROOT/scopy.py" "$ROOT/steam_recordings.py" "$STAGE/py_modules/"

(cd "$HERE/out" && python3 -m zipfile -c SCopy.zip SCopy)
echo "Built $HERE/out/SCopy.zip"
