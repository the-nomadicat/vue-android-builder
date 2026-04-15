#!/bin/bash
set -e

SDK_DIR="${ANDROID_HOME:-/mount/android-sdk}"
TOOLS_DIR="$SDK_DIR/cmdline-tools/latest"

echo "[init] ANDROID_HOME=$SDK_DIR"
echo "[init] JAVA_HOME=${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}"

if [ ! -f "$TOOLS_DIR/bin/sdkmanager" ]; then
    echo "[init] Android SDK not found — downloading command-line tools..."
    mkdir -p "$TOOLS_DIR"

    cd /tmp
    TOOLS_ZIP="commandlinetools-linux-11076708_latest.zip"
    echo "[init] Downloading $TOOLS_ZIP ..."
    curl -fsSL -o "$TOOLS_ZIP" \
        "https://dl.google.com/android/repository/$TOOLS_ZIP"

    echo "[init] Extracting..."
    unzip -q "$TOOLS_ZIP" -d /tmp/cmdtools-extract
    # The zip contains a 'cmdline-tools' directory; move its contents to latest/
    mv /tmp/cmdtools-extract/cmdline-tools/* "$TOOLS_DIR/"
    rm -rf /tmp/cmdtools-extract /tmp/"$TOOLS_ZIP"

    echo "[init] Accepting licenses..."
    yes | "$TOOLS_DIR/bin/sdkmanager" --sdk_root="$SDK_DIR" --licenses > /dev/null 2>&1 || true

    echo "[init] Installing SDK packages (build-tools;34.0.0, platforms;android-34)..."
    "$TOOLS_DIR/bin/sdkmanager" --sdk_root="$SDK_DIR" \
        "cmdline-tools;latest" \
        "platform-tools" \
        "build-tools;34.0.0" \
        "platforms;android-34"

    echo "[init] Android SDK installed."
else
    echo "[init] Android SDK already present at $TOOLS_DIR"
fi

# Ensure PATH includes SDK tools
export PATH="$TOOLS_DIR/bin:$SDK_DIR/platform-tools:$SDK_DIR/build-tools/34.0.0:$PATH"

echo "[init] Starting android-builder HTTP server..."
exec python3 /app/server.py
