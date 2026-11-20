#!/bin/bash
# Builds "Airwis.app": a double-clickable menu bar app that runs this folder's code.
# Usage: ./build_app.sh            (app is created in this folder)
#        ./build_app.sh --install  (also copies it to ~/Applications, so Spotlight finds it)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/Airwis.app"
PY="$HERE/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "Setting up the Python environment (first time only)..."
  python3 -m venv "$HERE/.venv"
  "$PY" -m pip install -q -r "$HERE/requirements.txt"
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Airwis</string>
  <key>CFBundleIdentifier</key><string>local.airwis</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <!-- menu bar only: no Dock icon -->
  <key>LSUIElement</key><true/>
  <key>NSCameraUsageDescription</key><string>Tracks your hand to move the mouse.</string>
</dict>
</plist>
EOF

# The project folder is baked in, so the app keeps working after you move it.
cat > "$APP/Contents/MacOS/launcher" <<EOF
#!/bin/bash
cd "$HERE"
exec "$PY" "$HERE/menubar.py"
EOF
chmod +x "$APP/Contents/MacOS/launcher"

# Ad-hoc signature, so macOS remembers the Camera / Accessibility permissions for this app.
codesign --force --sign - "$APP" >/dev/null 2>&1 || true

if [ "${1:-}" = "--install" ]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/Airwis.app"
  cp -R "$APP" "$HOME/Applications/"
  echo "Installed: ~/Applications/Airwis.app"
else
  echo "Built: $APP"
fi
