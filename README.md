# YT Relay

Public web page → your Mac's Downie → 0x0.st → public download link. Local copy
deleted after upload.

```
[ GH Pages frontend ]
       │  HTTPS
       ▼
[ Cloudflare Tunnel: yt.rentskill.ai ]
       │
       ▼
[ Python daemon on 127.0.0.1:8723 ]
       │ AppleScript                 ▲ curl -F file=@…
       ▼                             │
   [ Downie.app ] → file on disk → [ 0x0.st ]
                                     │
                                     ▼
                              local file deleted
```

Zero pip dependencies on the Mac. The frontend is a single static page.

---

## What lives where

```
mac/
  server.py            # the daemon — Python stdlib only
  install-launchd.sh   # writes & loads LaunchAgents for server + tunnel
frontend/
  index.html style.css app.js .nojekyll
scripts/
  setup-tunnel.sh      # creates the Cloudflare named tunnel
```

---

## One-time setup

### 1. Pick a passphrase
Long, random, kept in your shell.
```sh
export YT_RELAY_PASSPHRASE='something-long-and-uncopypasteable'
```
Put this in `~/.zshrc` (or wherever) so future shells inherit it.

### 2. Cloudflare Tunnel
You need the zone for the hostname you'll use (default `yt.rentskill.ai`)
already on Cloudflare. If you've never logged cloudflared into this account:
```sh
cloudflared tunnel login        # opens browser, pick the right zone
```
Then create the named tunnel and DNS:
```sh
bash scripts/setup-tunnel.sh
```
That writes `~/.cloudflared/yt-relay.yml` (a dedicated config file —
your existing `~/.cloudflared/config.yml` is untouched) and creates the
CNAME for `yt.rentskill.ai → <tunnel>.cfargotunnel.com`.

To use a different hostname:
```sh
HOSTNAME_FQDN=yt.example.com bash scripts/setup-tunnel.sh
```
and update the `BACKEND` constant at the top of [`frontend/app.js`](frontend/app.js).

Smoke-test it:
```sh
cloudflared tunnel run yt-relay &   # foreground in another tab is fine
python3 mac/server.py &
curl -sS https://yt.rentskill.ai/api/health
# → {"ok": true}
```

### 3. Make it auto-start
```sh
bash mac/install-launchd.sh
```
This drops two LaunchAgents in `~/Library/LaunchAgents/`:
- `gd.ink.yt-relay` — the Python daemon
- `gd.ink.yt-relay-tunnel` — cloudflared

Logs go to `~/Library/Logs/yt-relay/`. To stop:
```sh
launchctl unload ~/Library/LaunchAgents/gd.ink.yt-relay.plist
launchctl unload ~/Library/LaunchAgents/gd.ink.yt-relay-tunnel.plist
```

### 4. Downie
- The daemon hands off URLs via the documented verb
  `open all URLs in text "<url>"`. Downie's own Preferences → Downloads
  controls the destination folder; set `YT_RELAY_DOWNLOAD_DIR` (env var)
  to that same path so the daemon watches the correct folder.
- Default `YT_RELAY_DOWNLOAD_DIR` is `~/Downloads/yt-relay`. If you'd
  rather keep Downie's existing "Downie Downloads" folder, set
  `YT_RELAY_DOWNLOAD_DIR='/Users/<you>/Downloads/Downie Downloads'`.
- The first time the daemon AppleScripts Downie, macOS will prompt for
  Automation permission ("python3 wants to control Downie"). Click OK.
  If you miss the prompt, allow it manually under
  System Settings → Privacy & Security → Automation → python3 → Downie.
- Downie deduplicates against recently-completed downloads (in-app).
  Clearing the completed-downloads list in Downie's window will let you
  re-test the same URL.

### 5. Deploy the frontend
Push the `frontend/` directory to a GitHub repo and enable Pages on the
default branch. No build step. The `.nojekyll` file prevents GitHub from
processing it.

If you change the tunnel hostname later, edit the `BACKEND` constant at
the top of [`frontend/app.js`](frontend/app.js).

---

## How a request flows

1. Browser POSTs `{url, passphrase}` to `https://yt.rentskill.ai/api/download`.
2. Daemon checks passphrase (constant-time compare) and IP rate limit
   (SQLite: ≤ 20 reqs / IP / 24h, IPs older than 24h purged).
3. Daemon `osascript`s Downie with the URL, snapshots the watch folder.
4. A worker thread polls the folder; once a media file's size is unchanged
   for 4 s and isn't a `.part`/`.crdownload`, it's done.
5. Daemon `curl -F file=@…` to 0x0.st, gets back a URL.
6. Local file `unlink()`ed.
7. Frontend polling `/api/status/<id>` sees `status: done` and the public URL.

## Limits & gotchas

- 0x0.st caps at 512 MB. Bigger files fail at upload time with a clear error.
- 0x0.st's retention shrinks with size; very large files may only live a day
  or two. Smaller files live ~a month.
- The daemon supports YouTube, YouTube Shorts, youtu.be, m.youtube.com.
  Other sites are rejected by the URL regex. Loosen it in
  [`mac/server.py`](mac/server.py) if you want.
- Rate limiting is per IP, observed from `CF-Connecting-IP` (Cloudflare sets
  that). If you bypass the tunnel and hit localhost directly, it falls back
  to the socket peer.
- Downie is a singleton process. Concurrent jobs queue inside Downie itself.

## Troubleshooting

- `curl https://yt.rentskill.ai/api/health` → 404/timeout?
  - `launchctl list | grep yt-relay` should show both jobs.
  - `tail -f ~/Library/Logs/yt-relay/*.log` shows recent stderr.
  - `cloudflared tunnel info yt-relay` confirms the tunnel is registered.
- "Timed out waiting for Downie to produce a file"
  - Open Downie manually and confirm it can download that URL at all.
  - Confirm `YT_RELAY_DOWNLOAD_DIR` matches Downie's "Save downloads to"
    folder (Downie Preferences → Downloads).
  - Confirm Downie isn't silently deduplicating against a previous download
    of the same URL — clear its completed list and retry.
- "Access code rejected" in the browser
  - Confirm `YT_RELAY_PASSPHRASE` is set in the LaunchAgent. After editing,
    re-run `bash mac/install-launchd.sh` to reload it.
