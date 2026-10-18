#!/bin/bash
# Builds Goldmine.app (menu bar app) with swiftc; no Xcode project needed.
#   ./build.sh            build into app/build/
#   ./build.sh --install  also copy to ~/Applications and (re)launch it
#   ./build.sh --release  also upload the build to your Goldmine server (needs an admin client
#                         key in ~/.goldmine/config.toml); connected apps update themselves to it
# The app bundles the client (desktop/*.py), so an app update also updates it.
# Version: app/VERSION. Build number: UTC build time (YYYYMMDDHHMM), so later builds are newer.
set -euo pipefail
cd "$(dirname "$0")"

GOLDMINE_DIR="$(cd .. && pwd)"
PYTHON="${GOLDMINE_PYTHON:-$(command -v python3)}"
PYTHON="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$PYTHON")"
"$PYTHON" -c 'import sys, tomllib' 2>/dev/null || { echo "Need Python 3.11+ (set GOLDMINE_PYTHON)"; exit 1; }
VERSION="$(tr -d '[:space:]' < VERSION)"
BUILD="$(date -u +%Y%m%d%H%M)"

APP=build/Goldmine.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/desktop"

swiftc -O -swift-version 5 -parse-as-library \
    -target "$(uname -m)-apple-macos13" \
    Sources/*.swift -o "$APP/Contents/MacOS/Goldmine"

cp "$GOLDMINE_DIR"/desktop/*.py "$APP/Contents/Resources/desktop/"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Goldmine</string>
  <key>CFBundleIdentifier</key><string>net.snowskeleton.goldmine</string>
  <key>CFBundleExecutable</key><string>Goldmine</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${BUILD}</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>GoldminePython</key><string>${PYTHON}</string>
</dict>
</plist>
PLIST

codesign --force --sign - "$APP" >/dev/null
echo "built $APP ($VERSION, build $BUILD)"

case "${1:-}" in
--install)
    pkill -x Goldmine 2>/dev/null && sleep 1 || true
    mkdir -p ~/Applications
    rm -rf ~/Applications/Goldmine.app
    cp -R "$APP" ~/Applications/
    open ~/Applications/Goldmine.app
    echo "installed and launched ~/Applications/Goldmine.app"
    ;;
--release)
    "$PYTHON" "$GOLDMINE_DIR/desktop/goldmine.py" release-app "$APP" ${GOLDMINE_RELEASE_NOTES:+--notes "$GOLDMINE_RELEASE_NOTES"}
    ;;
esac
