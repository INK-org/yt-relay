#!/usr/bin/env python3
"""
YT-relay Mac daemon.

Receives a YouTube URL from the public frontend, hands it to Downie via
AppleScript, watches a dedicated downloads folder for the finished file,
uploads it to 0x0.st, deletes the local copy, and exposes the resulting
public URL back to the frontend.

Configuration is via environment variables (see config_from_env).
Zero pip dependencies — stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #

DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "yt-relay"
DEFAULT_DB_PATH = Path.home() / ".yt-relay" / "state.db"
DEFAULT_PORT = 8723

# A finished file's size must be unchanged for this many seconds.
STABILITY_SECONDS = 4
# Poll the watch folder this often.
POLL_INTERVAL = 1.0
# A job that produces no new file in this long is considered failed.
DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
# 0x0.st's hard cap.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
# Rate-limit window.
RATE_WINDOW_SECONDS = 24 * 60 * 60
RATE_LIMIT = 20

YT_URL_RE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?[^ ]*v=|shorts/|live/|embed/)"
    r"|youtu\.be/)[A-Za-z0-9_\-]{6,}",
    re.IGNORECASE,
)

MEDIA_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".mp3", ".m4a", ".aac", ".wav", ".flac"}
PARTIAL_SUFFIXES = (".part", ".crdownload", ".download", ".tmp", ".downie")


@dataclass(frozen=True)
class Config:
    port: int
    passphrase: str
    download_dir: Path
    db_path: Path
    allowed_origin: str


def config_from_env() -> Config:
    passphrase = os.environ.get("YT_RELAY_PASSPHRASE", "").strip()
    if not passphrase:
        sys.stderr.write("ERROR: YT_RELAY_PASSPHRASE must be set\n")
        sys.exit(2)
    return Config(
        port=int(os.environ.get("YT_RELAY_PORT", DEFAULT_PORT)),
        passphrase=passphrase,
        download_dir=Path(os.environ.get("YT_RELAY_DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR)).expanduser(),
        db_path=Path(os.environ.get("YT_RELAY_DB", DEFAULT_DB_PATH)).expanduser(),
        allowed_origin=os.environ.get("YT_RELAY_ORIGIN", "*"),
    )


# --------------------------------------------------------------------------- #
# Storage                                                                     #
# --------------------------------------------------------------------------- #

class Store:
    """SQLite wrapper with a single connection guarded by a lock.

    The schema is intentionally tiny: a rolling per-IP request log
    (purged at 24h) and a jobs table. We do not store IPs beyond the
    rate-limit window.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ip_log (
                  ip TEXT NOT NULL,
                  ts INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ip_log_ip_ts ON ip_log(ip, ts);

                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY,
                  status TEXT NOT NULL,
                  yt_url TEXT NOT NULL,
                  upload_url TEXT,
                  error TEXT,
                  file_path TEXT,
                  filename TEXT,
                  bytes INTEGER,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                );
                """
            )
            self._conn.commit()

    def purge_old(self) -> None:
        cutoff = int(time.time()) - RATE_WINDOW_SECONDS
        with self._lock:
            self._conn.execute("DELETE FROM ip_log WHERE ts < ?", (cutoff,))
            # Jobs older than 48h are also fine to drop; we already deleted the file.
            job_cutoff = int(time.time()) - 2 * RATE_WINDOW_SECONDS
            self._conn.execute("DELETE FROM jobs WHERE created_at < ?", (job_cutoff,))
            self._conn.commit()

    def count_recent(self, ip: str) -> int:
        cutoff = int(time.time()) - RATE_WINDOW_SECONDS
        with self._lock:
            cur = self._conn.execute(
                "SELECT count(*) AS n FROM ip_log WHERE ip = ? AND ts >= ?",
                (ip, cutoff),
            )
            return cur.fetchone()["n"]

    def log_request(self, ip: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO ip_log (ip, ts) VALUES (?, ?)",
                (ip, int(time.time())),
            )
            self._conn.commit()

    def create_job(self, job_id: str, yt_url: str) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, status, yt_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, "queued", yt_url, now, now),
            )
            self._conn.commit()

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {cols} WHERE id = ?",
                (*fields.values(), job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Downie + uploader                                                           #
# --------------------------------------------------------------------------- #

def trigger_downie(yt_url: str) -> None:
    """Tell Downie to download the URL.

    Downie's sdef does NOT expose a `download URL` command — the documented
    verb is `open all URLs in text "<text>"`, which parses URLs out of any
    string and adds them to Downie's queue. We pass a single URL.

    Downie's downloads folder is configured by the user in
    Downie -> Preferences -> Downloads; we just hand off the URL.
    """
    safe_url = yt_url.replace("\\", "\\\\").replace('"', '\\"')
    # `open all URLs in text` adds to the queue; `start queue` kicks it off
    # (no-op if already running). Without start queue, Downie may sit on
    # accumulated URLs without doing anything.
    script = (
        f'tell application "Downie"\n'
        f'    open all URLs in text "{safe_url}"\n'
        f'    start queue\n'
        f'end tell'
    )
    subprocess.run(
        ["osascript", "-e", script],
        check=True,
        capture_output=True,
        timeout=30,
    )


def snapshot_dir(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {p.name for p in path.iterdir() if p.is_file()}


def is_stable_media(path: Path) -> bool:
    if not path.is_file():
        return False
    if any(path.name.lower().endswith(s) for s in PARTIAL_SUFFIXES):
        return False
    if path.suffix.lower() not in MEDIA_EXTS:
        return False
    return True


def wait_for_new_file(
    watch_dir: Path,
    seen_before: set[str],
    started_at: float,
    deadline: float,
) -> Path | None:
    """Poll until a new, stable, media file appears in watch_dir.

    Returns the file path, or None if the deadline passes.
    """
    last_size: dict[str, tuple[int, float]] = {}
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        if not watch_dir.exists():
            continue
        for p in watch_dir.iterdir():
            if p.name in seen_before:
                continue
            if not is_stable_media(p):
                continue
            try:
                size = p.stat().st_size
            except FileNotFoundError:
                continue
            if size == 0:
                continue
            prev = last_size.get(p.name)
            now = time.time()
            if prev and prev[0] == size and (now - prev[1]) >= STABILITY_SECONDS:
                return p
            if not prev or prev[0] != size:
                last_size[p.name] = (size, now)
    return None


def upload_to_0x0(file_path: Path) -> str:
    """Upload via curl and return the public URL."""
    proc = subprocess.run(
        [
            "curl", "-sS", "--fail",
            "-F", f"file=@{file_path}",
            "-H", "User-Agent: yt-relay/1.0 (self-hosted)",
            "https://0x0.st",
        ],
        capture_output=True,
        text=True,
        timeout=60 * 30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"0x0.st upload failed: {proc.stderr.strip() or proc.stdout.strip()}")
    url = proc.stdout.strip()
    if not url.startswith("https://0x0.st/"):
        raise RuntimeError(f"unexpected response from 0x0.st: {url!r}")
    return url


# --------------------------------------------------------------------------- #
# Worker                                                                      #
# --------------------------------------------------------------------------- #

def run_job(job_id: str, yt_url: str, cfg: Config, store: Store) -> None:
    try:
        _run_job_inner(job_id, yt_url, cfg, store)
    except Exception as e:  # noqa: BLE001
        # Anything that escapes the inner function (e.g. PermissionError on
        # the watch folder before macOS grants Full Disk Access) becomes a
        # clean error status instead of silently killing the worker thread.
        store.update_job(job_id, status="error", error=f"worker crashed: {e}")


def _run_job_inner(job_id: str, yt_url: str, cfg: Config, store: Store) -> None:
    store.update_job(job_id, status="downloading")
    try:
        cfg.download_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        store.update_job(
            job_id,
            status="error",
            error=f"Cannot access {cfg.download_dir}: {e}. Grant Full Disk Access to /usr/bin/python3 in System Settings → Privacy & Security, then `launchctl unload && load` the daemon.",
        )
        return
    seen_before = snapshot_dir(cfg.download_dir)
    started_at = time.time()
    try:
        trigger_downie(yt_url)
    except subprocess.CalledProcessError as e:
        store.update_job(
            job_id,
            status="error",
            error=f"Downie AppleScript failed: {e.stderr.decode('utf-8', 'replace').strip()[:500]}",
        )
        return
    except Exception as e:  # noqa: BLE001
        store.update_job(job_id, status="error", error=f"Downie trigger failed: {e}")
        return

    deadline = started_at + DOWNLOAD_TIMEOUT_SECONDS
    new_file = wait_for_new_file(cfg.download_dir, seen_before, started_at, deadline)
    if not new_file:
        store.update_job(
            job_id,
            status="error",
            error=f"Timed out waiting for Downie to produce a file in {cfg.download_dir} after {DOWNLOAD_TIMEOUT_SECONDS}s.",
        )
        return

    size = new_file.stat().st_size
    store.update_job(
        job_id,
        filename=new_file.name,
        file_path=str(new_file),
        bytes=size,
    )

    if size > MAX_UPLOAD_BYTES:
        try:
            new_file.unlink(missing_ok=True)
        except OSError:
            pass
        store.update_job(
            job_id,
            status="error",
            error=f"File is {size // (1024 * 1024)} MB which exceeds 0x0.st's 512 MB limit.",
        )
        return

    store.update_job(job_id, status="uploading")
    try:
        url = upload_to_0x0(new_file)
    except Exception as e:  # noqa: BLE001
        store.update_job(job_id, status="error", error=f"Upload failed: {e}")
        return

    try:
        new_file.unlink(missing_ok=True)
    except OSError as e:
        # Upload succeeded; deletion is best-effort.
        store.update_job(
            job_id,
            status="done",
            upload_url=url,
            error=f"(note) local file delete failed: {e}",
        )
        return

    store.update_job(job_id, status="done", upload_url=url)


# --------------------------------------------------------------------------- #
# HTTP                                                                        #
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    # set by serve()
    cfg: Config = None  # type: ignore[assignment]
    store: Store = None  # type: ignore[assignment]

    server_version = "yt-relay/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.address_string()} {fmt % args}\n"
        )

    # -- CORS helpers ------------------------------------------------------- #

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.cfg.allowed_origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Passphrase")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    # -- Routing ------------------------------------------------------------ #

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if parsed.path.startswith("/api/status/"):
            job_id = parsed.path[len("/api/status/"):]
            job = self.store.get_job(job_id)
            if not job:
                self._json(HTTPStatus.NOT_FOUND, {"error": "unknown job"})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "id": job["id"],
                    "status": job["status"],
                    "upload_url": job["upload_url"],
                    "error": job["error"],
                    "filename": job["filename"],
                    "bytes": job["bytes"],
                },
            )
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/download":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        # Read body
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad content-length"})
            return
        if length <= 0 or length > 8192:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "missing or oversized body"})
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return

        passphrase = (body.get("passphrase") or self.headers.get("X-Passphrase") or "").strip()
        yt_url = (body.get("url") or "").strip()

        if not secrets.compare_digest(passphrase, self.cfg.passphrase):
            # Brief sleep blunts brute-force attempts at the application level.
            time.sleep(0.5)
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "bad passphrase"})
            return

        if not YT_URL_RE.match(yt_url):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "only YouTube URLs are accepted"})
            return

        ip = self._client_ip()
        if self.store.count_recent(ip) >= RATE_LIMIT:
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": f"limit is {RATE_LIMIT} downloads per IP per 24h"},
            )
            return
        self.store.log_request(ip)

        job_id = secrets.token_urlsafe(12)
        self.store.create_job(job_id, yt_url)

        worker = threading.Thread(
            target=run_job,
            args=(job_id, yt_url, self.cfg, self.store),
            daemon=True,
            name=f"job-{job_id}",
        )
        worker.start()

        self._json(HTTPStatus.ACCEPTED, {"id": job_id, "status": "queued"})

    # -- Helpers ------------------------------------------------------------ #

    def _client_ip(self) -> str:
        # cloudflared sets CF-Connecting-IP for the originating client.
        cf = self.headers.get("CF-Connecting-IP")
        if cf:
            return cf.split(",")[0].strip()
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #

def janitor(store: Store) -> None:
    while True:
        time.sleep(600)
        try:
            store.purge_old()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"janitor error: {e}\n")


def serve() -> None:
    cfg = config_from_env()
    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)

    Handler.cfg = cfg
    Handler.store = store

    threading.Thread(target=janitor, args=(store,), daemon=True, name="janitor").start()

    addr = ("127.0.0.1", cfg.port)
    httpd = ThreadingHTTPServer(addr, Handler)
    sys.stderr.write(
        f"yt-relay listening on http://{addr[0]}:{addr[1]}  "
        f"download_dir={cfg.download_dir}  origin={cfg.allowed_origin}\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    serve()
