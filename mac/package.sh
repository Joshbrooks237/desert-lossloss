#!/bin/sh
set -e
cd "$(dirname "$0")/.."
ROOT=$(pwd)
VENDOR="$ROOT/mac/vendor"
APP="$ROOT/mac/build/LOSSLOSS.app"
DIST="$ROOT/mac/dist"
mkdir -p "$VENDOR" "$DIST"

fetch() {
  dest="$1"
  url="$2"
  if [ -f "$dest" ]; then
    return 0
  fi
  echo "Downloading $(basename "$dest")"
  curl -fL --retry 3 -o "$dest.partial" "$url"
  mv "$dest.partial" "$dest"
}

fetch "$VENDOR/cpython-3.12.16-x86_64.tar.gz" \
  "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-x86_64-apple-darwin-install_only_stripped.tar.gz"
fetch "$VENDOR/cpython-3.12.16-arm64.tar.gz" \
  "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-aarch64-apple-darwin-install_only_stripped.tar.gz"
fetch "$VENDOR/ffmpeg-9.0.2.zip" "https://evermeet.cx/ffmpeg/ffmpeg-9.0.2.zip"
fetch "$VENDOR/ffprobe-9.0.2.zip" "https://evermeet.cx/ffmpeg/ffprobe-9.0.2.zip"
fetch "$VENDOR/ffmpeg-darwin-arm64.gz" \
  "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/ffmpeg-darwin-arm64.gz"
fetch "$VENDOR/ffprobe-darwin-arm64.gz" \
  "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/ffprobe-darwin-arm64.gz"

if [ ! -f "$VENDOR/ffmpeg-darwin-arm64" ]; then
  gunzip -kf "$VENDOR/ffmpeg-darwin-arm64.gz"
fi
if [ ! -f "$VENDOR/ffprobe-darwin-arm64" ]; then
  gunzip -kf "$VENDOR/ffprobe-darwin-arm64.gz"
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
echo "Compiling the window"
swiftc -O -target x86_64-apple-macosx12.0 -framework Cocoa -framework WebKit -o "$APP/Contents/MacOS/LOSSLOSS-x86_64" mac/window.swift
swiftc -O -target arm64-apple-macosx12.0 -framework Cocoa -framework WebKit -o "$APP/Contents/MacOS/LOSSLOSS-arm64" mac/window.swift
lipo -create -output "$APP/Contents/MacOS/LOSSLOSS" \
  "$APP/Contents/MacOS/LOSSLOSS-x86_64" \
  "$APP/Contents/MacOS/LOSSLOSS-arm64"
rm -f "$APP/Contents/MacOS/LOSSLOSS-x86_64" "$APP/Contents/MacOS/LOSSLOSS-arm64"
cp mac/Info.plist "$APP/Contents/Info.plist"
cp -R player "$APP/Contents/Resources/player"
rm -rf "$APP/Contents/Resources/player/__pycache__"

echo "Packing Python and ffmpeg"
mkdir -p "$APP/Contents/Resources/python/x86_64" "$APP/Contents/Resources/python/arm64"
tar -xzf "$VENDOR/cpython-3.12.16-x86_64.tar.gz" -C "$APP/Contents/Resources/python/x86_64" --strip-components=1
tar -xzf "$VENDOR/cpython-3.12.16-arm64.tar.gz" -C "$APP/Contents/Resources/python/arm64" --strip-components=1
mkdir -p "$APP/Contents/Resources/bin/x86_64" "$APP/Contents/Resources/bin/arm64"
unzip -qo "$VENDOR/ffmpeg-9.0.2.zip" -d "$APP/Contents/Resources/bin/x86_64"
unzip -qo "$VENDOR/ffprobe-9.0.2.zip" -d "$APP/Contents/Resources/bin/x86_64"
cp "$VENDOR/ffmpeg-darwin-arm64" "$APP/Contents/Resources/bin/arm64/ffmpeg"
cp "$VENDOR/ffprobe-darwin-arm64" "$APP/Contents/Resources/bin/arm64/ffprobe"
chmod +x "$APP/Contents/Resources/bin/x86_64/ffmpeg" "$APP/Contents/Resources/bin/x86_64/ffprobe" \
  "$APP/Contents/Resources/bin/arm64/ffmpeg" "$APP/Contents/Resources/bin/arm64/ffprobe"

python3 - << 'PY'
import math, struct, zlib
from pathlib import Path

def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

def png(size):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            nx = (x + 0.5) / size - 0.5
            ny = (y + 0.5) / size - 0.5
            disk = math.hypot(nx - 0.08, ny - 0.02)
            if disk < 0.16:
                raw.extend((226, 91, 42, 255))
            elif abs(ny - 0.22) < 0.012 and abs(nx) < 0.34:
                raw.extend((198, 161, 90, 255))
            else:
                raw.extend((17, 14, 12, 255))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")

Path("/tmp/lossloss-icon.png").write_bytes(png(1024))
PY
ICONSET="mac/build/LOSSLOSS.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for spec in "16:icon_16x16.png" "32:icon_16x16@2x.png" "32:icon_32x32.png" "64:icon_32x32@2x.png" "128:icon_128x128.png" "256:icon_128x128@2x.png" "256:icon_256x256.png" "512:icon_256x256@2x.png" "512:icon_512x512.png" "1024:icon_512x512@2x.png"; do
  size="${spec%%:*}"
  name="${spec#*:}"
  sips -z "$size" "$size" /tmp/lossloss-icon.png --out "$ICONSET/$name" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/LOSSLOSS.icns"
rm -rf "$ICONSET"

echo "Signing"
xattr -cr "$APP"
find "$APP" -type f > /tmp/lossloss-sign-list
while IFS= read -r file; do
  kind=$(file -b "$file")
  case "$kind" in
    Mach-O*) codesign --force --sign - "$file" ;;
  esac
done < /tmp/lossloss-sign-list
rm -f /tmp/lossloss-sign-list
codesign --force --sign - "$APP"

DEST="$HOME/Applications/LOSSLOSS.app"
mkdir -p "$HOME/Applications"
rm -rf "$DEST"
cp -R "$APP" "$DEST"

STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/LOSSLOSS.app"
ln -s /Applications "$STAGE/Applications"
RW="$DIST/LOSSLOSS-rw.dmg"
rm -f "$RW" "$DIST/LOSSLOSS.dmg"
echo "Building the disk image"
hdiutil create -volname LOSSLOSS -srcfolder "$STAGE" -ov -format UDRW "$RW" >/dev/null
if [ -d /Volumes/LOSSLOSS ]; then
  hdiutil detach /Volumes/LOSSLOSS >/dev/null || true
fi
hdiutil attach -readwrite -noverify -noautoopen "$RW" >/dev/null
sleep 2
osascript <<'EOF' || echo "Finder layout skipped; the disk image still has both icons."
tell application "Finder"
  tell disk "LOSSLOSS"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {240, 120, 820, 460}
    set viewOptions to the icon view options of container window
    set arrangement of viewOptions to not arranged
    set icon size of viewOptions to 96
    set position of item "LOSSLOSS.app" of container window to {150, 170}
    set position of item "Applications" of container window to {430, 170}
    close
    open
    update without registering applications
    delay 2
  end tell
end tell
EOF
sync
hdiutil detach /Volumes/LOSSLOSS >/dev/null
hdiutil convert "$RW" -format UDZO -ov -o "$DIST/LOSSLOSS.dmg" >/dev/null
rm -f "$RW"
rm -rf "$STAGE"
echo "Built $DEST"
echo "Built $DIST/LOSSLOSS.dmg"
