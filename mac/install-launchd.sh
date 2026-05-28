#!/usr/bin/env bash
# Install + load the two LaunchAgents that keep yt-relay running:
#   - gd.ink.yt-relay        — the Python daemon
#   - gd.ink.yt-relay-tunnel — cloudflared tunnel
#
# Re-run after editing PASSPHRASE or other env vars below.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_DIR="${HOME}/Library/LaunchAgents"
LOG_DIR="${HOME}/Library/Logs/yt-relay"
mkdir -p "${LAUNCH_DIR}" "${LOG_DIR}"

if [ -z "${YT_RELAY_PASSPHRASE:-}" ]; then
    echo "Set YT_RELAY_PASSPHRASE in your shell first, e.g.:" >&2
    echo "  export YT_RELAY_PASSPHRASE='something-long'" >&2
    exit 2
fi

# Optional overrides.
PORT="${YT_RELAY_PORT:-8723}"
DOWNLOAD_DIR="${YT_RELAY_DOWNLOAD_DIR:-${HOME}/Downloads/yt-relay}"
ALLOWED_ORIGIN="${YT_RELAY_ORIGIN:-*}"

CLOUDFLARED_BIN="$(command -v cloudflared || true)"
if [ -z "${CLOUDFLARED_BIN}" ]; then
    echo "cloudflared not on PATH." >&2
    exit 1
fi

PYTHON_BIN="$(command -v python3)"

SERVER_PLIST="${LAUNCH_DIR}/gd.ink.yt-relay.plist"
TUNNEL_PLIST="${LAUNCH_DIR}/gd.ink.yt-relay-tunnel.plist"

cat > "${SERVER_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>gd.ink.yt-relay</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${REPO_DIR}/mac/server.py</string>
    </array>
    <key>WorkingDirectory</key><string>${REPO_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>YT_RELAY_PASSPHRASE</key><string>${YT_RELAY_PASSPHRASE}</string>
        <key>YT_RELAY_PORT</key><string>${PORT}</string>
        <key>YT_RELAY_DOWNLOAD_DIR</key><string>${DOWNLOAD_DIR}</string>
        <key>YT_RELAY_ORIGIN</key><string>${ALLOWED_ORIGIN}</string>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>${LOG_DIR}/server.out.log</string>
    <key>StandardErrorPath</key><string>${LOG_DIR}/server.err.log</string>
</dict>
</plist>
EOF

cat > "${TUNNEL_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>gd.ink.yt-relay-tunnel</string>
    <key>ProgramArguments</key>
    <array>
        <string>${CLOUDFLARED_BIN}</string>
        <string>--config</string>
        <string>${HOME}/.cloudflared/yt-relay.yml</string>
        <string>tunnel</string>
        <string>run</string>
        <string>yt-relay</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>TUNNEL_ORIGIN_CERT</key><string>${HOME}/.cloudflared/cert.pem</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>${LOG_DIR}/tunnel.out.log</string>
    <key>StandardErrorPath</key><string>${LOG_DIR}/tunnel.err.log</string>
</dict>
</plist>
EOF

# Unload first so updated env vars take effect.
launchctl unload "${SERVER_PLIST}" 2>/dev/null || true
launchctl unload "${TUNNEL_PLIST}" 2>/dev/null || true
launchctl load "${SERVER_PLIST}"
launchctl load "${TUNNEL_PLIST}"

echo "Loaded:"
echo "  ${SERVER_PLIST}"
echo "  ${TUNNEL_PLIST}"
echo
echo "Logs:  ${LOG_DIR}"
echo "Stop:  launchctl unload <plist>"
