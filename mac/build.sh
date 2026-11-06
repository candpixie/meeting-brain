#!/bin/zsh
# Builds MeetingBrain.app and installs it to ~/Applications.
# Signed with a real identity so the Full Disk Access grant survives rebuilds.
set -e
cd "${0:A:h}"
swift build -c release
APP=build/MeetingBrain.app
rm -rf $APP && mkdir -p $APP/Contents/MacOS $APP/Contents/Resources
cp .build/release/MeetingBrain $APP/Contents/MacOS/
# app icon from icon/icon_1024.png (redraw with: swiftc icon/make_icon.swift -o /tmp/mi && /tmp/mi icon/icon_1024.png)
SET=build/AppIcon.iconset && rm -rf $SET && mkdir -p $SET
for s in 16 32 128 256 512; do
  sips -z $s $s icon/icon_1024.png --out $SET/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) icon/icon_1024.png --out $SET/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns $SET -o $APP/Contents/Resources/AppIcon.icns
cat > $APP/Contents/Info.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>io.candpixie.meetingbrain</string>
  <key>CFBundleName</key><string>Meeting Brain</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleExecutable</key><string>MeetingBrain</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
ID=$(security find-identity -v -p codesigning | grep -m1 "Apple Development" | awk '{print $2}')
codesign --force --sign "${ID:--}" $APP
mkdir -p ~/Applications && rm -rf ~/Applications/MeetingBrain.app && cp -R $APP ~/Applications/
echo "installed ~/Applications/MeetingBrain.app"
