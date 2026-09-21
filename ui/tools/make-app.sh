#!/bin/bash
# Assemble and sign daa.app. No Xcode required: swiftc + SwiftPM + codesign.
#
# Signing is the one thing here that is not cosmetic. TCC keys every privacy
# grant to the app's DESIGNATED REQUIREMENT:
#
#   ad-hoc signature   -> the DR *is* the cdhash, so it changes on every
#                         rebuild and every grant is thrown away
#   certificate        -> the DR pins identifier + anchor apple generic +
#                         subject.OU, none of which move when you rebuild
#
# So set DAA_SIGN_IDENTITY to an "Apple Development" identity as soon as you
# have one. Until then the app will tell the user, at launch and in plain
# words, that its permissions are temporary and why.
#
#   DAA_SIGN_IDENTITY="Apple Development: you@example.com (TEAMID)" ./make-app.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
CONFIG="${DAA_CONFIG:-release}"
APP="${DAA_APP_DIR:-$ROOT/build}/daa.app"
IDENTITY="${DAA_SIGN_IDENTITY:--}"

echo "==> building ($CONFIG)"
swift build -c "$CONFIG" --product daadock
BIN="$(swift build -c "$CONFIG" --show-bin-path)/daadock"

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/daadock"
cp "$ROOT/bundle/Info.plist" "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

echo "==> signing with: $IDENTITY"
# Hardened runtime from the start. Entitlements are verified after signing
# because `codesign` has been observed to silently drop them (which is also
# why bundle/daa.entitlements contains no XML comments).
codesign --force --timestamp=none \
         --options runtime \
         --entitlements "$ROOT/bundle/daa.entitlements" \
         --sign "$IDENTITY" \
         "$APP"

echo "==> verifying"
codesign --verify --deep --strict --verbose=2 "$APP"
echo "--- entitlements actually applied ---"
codesign -d --entitlements - "$APP" 2>/dev/null || true
echo "--- designated requirement ---"
# This is the line that decides whether grants survive. Diff it across two
# builds: if it says `cdhash H"..."` it will change every time.
codesign --display -r - "$APP" 2>&1 | sed -n 's/^# designated => //p'

if [ "$IDENTITY" = "-" ]; then
  cat <<'WARN'

  !!  This build is AD-HOC SIGNED.
      Its designated requirement is its own code hash, so macOS will treat the
      next build as a different app and forget every permission you grant it.
      The app detects this itself and says so at launch; that notice is not a
      bug report, it is this line.

      Fix: join the Apple Developer Program, then
        DAA_SIGN_IDENTITY="Apple Development: ..." ./tools/make-app.sh

WARN
fi

echo "==> $APP"
