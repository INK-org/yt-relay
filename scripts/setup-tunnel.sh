#!/usr/bin/env bash
# Create the named Cloudflare Tunnel `yt-relay` and wire it to
# https://yt.rentskill.ai  →  http://127.0.0.1:8723.
#
# Prereqs (you do these once, in a browser, before running this):
#   1) `brew install cloudflared` (you already have it)
#   2) `cloudflared tunnel login` — pick the ink.gd zone in the browser.
#      That drops a cert at ~/.cloudflared/cert.pem.
#
# Re-running is safe: each step checks for prior state.

set -euo pipefail

TUNNEL_NAME="${TUNNEL_NAME:-yt-relay}"
HOSTNAME="${HOSTNAME_FQDN:-yt.rentskill.ai}"
LOCAL_PORT="${YT_RELAY_PORT:-8723}"
CF_DIR="${HOME}/.cloudflared"

if ! command -v cloudflared >/dev/null 2>&1; then
    echo "cloudflared not found. brew install cloudflared first." >&2
    exit 1
fi

if [ ! -f "${CF_DIR}/cert.pem" ]; then
    echo "No ${CF_DIR}/cert.pem. Run:  cloudflared tunnel login" >&2
    echo "Then re-run this script." >&2
    exit 1
fi

# Create tunnel if missing. The list output looks like:
#   ID                                   NAME    CREATED ...
existing_id="$(cloudflared tunnel list --output json 2>/dev/null \
    | /usr/bin/python3 -c "import json,sys; rows=json.load(sys.stdin); print(next((r['id'] for r in rows if r['name']=='${TUNNEL_NAME}'), ''))")"

if [ -z "${existing_id}" ]; then
    echo "Creating tunnel ${TUNNEL_NAME}..."
    cloudflared tunnel create "${TUNNEL_NAME}"
    existing_id="$(cloudflared tunnel list --output json \
        | /usr/bin/python3 -c "import json,sys; rows=json.load(sys.stdin); print(next(r['id'] for r in rows if r['name']=='${TUNNEL_NAME}'))")"
else
    echo "Tunnel ${TUNNEL_NAME} already exists (${existing_id})."
fi

# Find the credentials JSON cloudflared just wrote.
CREDS="${CF_DIR}/${existing_id}.json"
if [ ! -f "${CREDS}" ]; then
    echo "Expected credentials at ${CREDS} but did not find them." >&2
    exit 1
fi

# Write the dedicated config FIRST. We need it so we can pass --config to
# the route command — otherwise cloudflared falls back to
# ~/.cloudflared/config.yml's `tunnel:` line and routes to the WRONG tunnel.
CFG="${CF_DIR}/yt-relay.yml"
cat > "${CFG}" <<EOF
tunnel: ${existing_id}
credentials-file: ${CREDS}

ingress:
  - hostname: ${HOSTNAME}
    service: http://127.0.0.1:${LOCAL_PORT}
    originRequest:
      noTLSVerify: true
      connectTimeout: 30s
  - service: http_status:404
EOF
echo "Wrote ${CFG}."

# Route DNS. --overwrite-dns replaces any existing record at this hostname
# (idempotent across reruns / recovery from earlier wrong CNAMEs).
echo "Routing ${HOSTNAME} → ${TUNNEL_NAME} (${existing_id})..."
cloudflared --config "${CFG}" tunnel route dns --overwrite-dns "${TUNNEL_NAME}" "${HOSTNAME}"

echo
echo "Done. Test with:"
echo "  cloudflared --config ${CFG} tunnel run ${TUNNEL_NAME}"
echo "  curl -sS https://${HOSTNAME}/api/health"
echo
echo "To run as a launchd agent on login, see mac/install-launchd.sh."
