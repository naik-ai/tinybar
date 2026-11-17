#!/bin/bash
# Builds a universal Modelbar.app into build/, ad-hoc signed by default.
#
# This is the fork (ADR 0015): its own name, executable (Modelbar) and bundle identifier
# (ai.naik.modelbar), so it runs beside an installed upstream Tinybar.app and never takes its
# preferences, data folder, login item or updates. The build stops if the bundle would carry
# upstream's identifier.
#
# For distribution, set SIGN_IDENTITY="Developer ID Application: …". With notary credentials too,
# the app is notarized and stapled: NOTARY_PROFILE (a `xcrun notarytool store-credentials` profile),
# or an App Store Connect API key as NOTARY_KEY (path to the .p8), NOTARY_KEY_ID and NOTARY_ISSUER.
# `--zip` also writes build/Modelbar-<version>.zip.
set -euo pipefail
cd "$(dirname "$0")/.."

swift build -c release --product Tinybar --arch arm64 --arch x86_64
# Where the universal binary lands depends on the toolchain (.build/apple/… or .build/out/…).
BIN="$(swift build -c release --product Tinybar --arch arm64 --arch x86_64 --show-bin-path)/Tinybar"

PLIST=Resources/Info.plist
BUNDLE_ID=$(/usr/libexec/PlistBuddy -c "Print CFBundleIdentifier" "$PLIST")
EXECUTABLE=$(/usr/libexec/PlistBuddy -c "Print CFBundleExecutable" "$PLIST")
NAME=$(/usr/libexec/PlistBuddy -c "Print CFBundleName" "$PLIST")
if [[ "$BUNDLE_ID" == "com.tinybar.app" || "$NAME" == "Tinybar" || "$EXECUTABLE" == "Tinybar" ]]; then
    echo "Refusing to build the fork with upstream Tinybar's identity ($BUNDLE_ID, $NAME, $EXECUTABLE)" >&2
    exit 1
fi

APP="build/$NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/$EXECUTABLE"
cp "$PLIST" "$APP/Contents/Info.plist"
cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

if [[ -n "${SIGN_IDENTITY:-}" ]]; then
    codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$APP"
else
    codesign --force --options runtime --timestamp=none --sign - "$APP"
fi
echo "Built $APP"

if [[ "${1:-}" == "--zip" ]]; then
    VERSION=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" Resources/Info.plist)
    ZIP="build/$EXECUTABLE-$VERSION.zip"
    if [[ -n "${NOTARY_PROFILE:-}" ]]; then
        NOTARY=(--keychain-profile "$NOTARY_PROFILE")
    elif [[ -n "${NOTARY_KEY:-}" ]]; then
        NOTARY=(--key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER")
    fi
    if [[ -n "${SIGN_IDENTITY:-}" && -n "${NOTARY+x}" ]]; then
        ditto -c -k --keepParent "$APP" "$ZIP"
        xcrun notarytool submit "$ZIP" "${NOTARY[@]}" --wait
        # Apple's ticket server sometimes drops the connection right after notarization.
        for attempt in 1 2 3 4 5; do
            xcrun stapler staple "$APP" && break
            [[ $attempt == 5 ]] && exit 1
            sleep 15
        done
    fi
    rm -f "$ZIP"
    ditto -c -k --keepParent "$APP" "$ZIP"
    echo "Built $ZIP"
fi
