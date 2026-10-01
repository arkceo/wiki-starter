#!/usr/bin/env python3
"""wiki_server.py — serve the wiki to this Mac, and only to this Mac.

Run by a LaunchAgent with KeepAlive. Two origins, on two ports of 127.0.0.1:

  the site (port, default 8765)
    /                  the built site in public/ (Quartz clean URLs: /page -> page.html,
                       /folder/ -> folder/index.html)
    /raw/<path>        source documents, read-only, so "Sources" links open the original
    /archive/<path>    processed Update Packets, read-only
    /upload            redirects to the Upload app
  the Upload app (uploadPort, default port + 1)
    /upload            the Upload & Logs page (engine/upload/upload.html)
    /api/upload        POST one file into the queue (raw/_intake/, or raw/inbox/ for a packet)
    /api/events        the activity log as a live stream (server-sent events), and what
                       is happening right now as `now` events
    /api/status        what is queued, running and waiting for review
    /api/process       POST: start the runner now

Both bind to 127.0.0.1, so nothing on the network can reach them. The rest stops a web
page, from another site or from inside the wiki, using the browser against them:
  - every request must name the server in its Host header (defeats DNS rebinding);
  - the Upload app is a different origin from the site, so nothing served on the site
    (an uploaded HTML document, a page written from one) can read its token;
  - every /api/ call must carry the token baked into the Upload page at start-up, and a
    POST from a browser must come from the Upload app's own origin;
  - an original that a browser could run (HTML, SVG, XML, ...) is sent as a download in a
    sandbox, never shown inline.
Requests that try to escape the served folders (../, encoded or not, or a symlink
pointing outside) are refused. Nothing here looks up a name or reaches another device on
the network (scripts/wiki_netguard.py refuses it), so macOS has no reason to ask for
local network access. Standard library only.

Usage: wiki_server.py [--port N] [--upload-port N]   (default: wiki.config.json)
"""
import argparse
import filecmp
import hmac
import threading
import http.server
import json
import mimetypes
import os
import posixpath
import re
import secrets
import socketserver
import subprocess
import sys
import time
import urllib.parse

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WIKI_DIR, "scripts"))
import wiki_events  # noqa: E402
import wiki_netguard  # noqa: E402

BIND = "127.0.0.1"
ROOTS = {"raw": "raw", "archive": "archive"}  # url prefix -> folder under WIKI_DIR
EXTRA_TYPES = {".md": "text/markdown; charset=utf-8", ".csv": "text/csv; charset=utf-8"}
# Originals a browser shows inline. Anything else (HTML, SVG, XML, Office, unknown) is sent
# as a download, so a document can never run script in the wiki's origin.
INLINE_TYPES = {"application/pdf", "text/plain", "text/markdown", "text/csv", "image/png",
                "image/jpeg", "image/gif", "image/webp", "image/heic", "image/tiff", "image/bmp",
                "audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/x-wav",
                "video/mp4", "video/quicktime"}
NOT_BUILT = (
    "<!doctype html><meta charset=utf-8><title>Wiki</title>"
    "<body style='font:16px -apple-system,sans-serif;max-width:36rem;margin:4rem auto'>"
    "<h1>The wiki is being built</h1><p>The first build takes a minute or two. "
    "Refresh this page shortly.</p></body>"
)
UPLOAD_PAGE = os.path.join(WIKI_DIR, "engine", "upload", "upload.html")
STATE_DIR = os.path.join(WIKI_DIR, ".wiki-engine", "state")
UPLOAD_TMP = os.path.join(STATE_DIR, "uploads")
LOCK_DIR = os.path.join(STATE_DIR, "runner.lock")
QUEUES = {"intake": "raw/_intake", "inbox": "raw/inbox"}
# Same test as looks_like_packet in wiki_runner.sh.
PACKET_RE = re.compile(rb"^## \[[0-9]{4}-[0-9]{2}-[0-9]{2}\] *update *\|", re.M)
PARTIAL_EXT = (".download", ".crdownload", ".part", ".partial", ".tmp")
TOKEN = secrets.token_urlsafe(24)
HISTORY_LINES = 800
HEARTBEAT_SECONDS = 15


def load_config():
    try:
        with open(os.path.join(WIKI_DIR, "wiki.config.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def default_port():
    try:
        return int(load_config().get("port") or 8765)
    except (TypeError, ValueError):
        return 8765


def default_upload_port(port):
    try:
        return int(load_config().get("uploadPort") or port + 1)
    except (TypeError, ValueError):
        return port + 1


def max_upload_bytes():
    try:
        return int(float(load_config().get("maxUploadMb") or 500) * 1024 * 1024)
    except (TypeError, ValueError):
        return 500 * 1024 * 1024


def safe_join(base, rel):
    """Resolve rel under base. Return None if the result would leave base."""
    base_real = os.path.realpath(base)
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." or "\x00" in p or "\\" in p for p in parts):
        return None
    target = os.path.realpath(os.path.join(base_real, *parts))
    if target != base_real and not target.startswith(base_real + os.sep):
        return None
    return target


# --------------------------------------------------------------- upload names ------
def clean_segment(seg):
    """One path segment as a safe file name, or "" if nothing usable is left."""
    seg = "".join(ch for ch in seg if ch >= " " and ch not in "\x7f\\:")
    seg = seg.strip().lstrip(".").strip()
    if len(seg.encode("utf-8")) > 200:
        stem, ext = os.path.splitext(seg)
        ext = ext if len(ext) <= 16 else ""
        while len((stem + ext).encode("utf-8")) > 200:
            stem = stem[:-1]
        seg = stem.rstrip() + ext
    return seg


def clean_upload_name(raw_name):
    """Validate the name the browser sent (a file name, or a relative path for a folder
    drop). Returns (relative path, None) or (None, reason it was skipped)."""
    if not raw_name:
        return None, "no file name"
    parts = [p for p in raw_name.replace("\\", "/").split("/") if p.strip() not in ("", ".")]
    if not parts or any(p.strip() == ".." for p in parts):
        return None, "not a valid file name"
    base = parts[-1]
    # Hidden folders too: a dropped USB stick or project brings .Spotlight-V100, .git, ...
    if any(p.strip().startswith(".") for p in parts) or base.startswith("~$") \
            or base.lower().endswith(PARTIAL_EXT):
        return None, "a hidden, system or temporary file"
    cleaned = [clean_segment(p) for p in parts[-4:]]  # keep at most three folder levels
    if not all(cleaned):
        return None, "not a valid file name"
    # The runner skips README.md and names starting with "Icon" (Finder's own files).
    if cleaned[-1] == "README.md" or cleaned[-1].startswith("Icon"):
        cleaned[-1] = "uploaded-" + cleaned[-1]
    return "/".join(cleaned), None


def is_packet(path, name):
    if not name.lower().endswith((".md", ".markdown")):
        return False
    try:
        with open(path, "rb") as f:
            return bool(PACKET_RE.search(f.read(400)))
    except OSError:
        return False


def place_upload(tmp, name):
    """Move a finished upload into its queue without ever replacing a file.
    Returns (queue, final path, duplicate)."""
    queue = "inbox" if is_packet(tmp, name) else "intake"
    # Packets are applied one by one and stay flat; documents keep their folder.
    sub = os.path.basename(name) if queue == "inbox" else name
    first = os.path.join(WIKI_DIR, QUEUES[queue], *sub.split("/"))
    os.makedirs(os.path.dirname(first), exist_ok=True)
    stem, ext = os.path.splitext(first)
    for n in range(1, 1000):
        dest = first if n == 1 else f"{stem} ({n}){ext}"
        try:
            os.link(tmp, dest)  # atomic, and fails instead of overwriting
        except FileExistsError:
            if os.path.isfile(dest) and filecmp.cmp(tmp, dest, shallow=False):
                os.unlink(tmp)
                return queue, dest, True
            continue
        os.unlink(tmp)
        return queue, dest, False
    raise OSError("too many files with this name")


# --------------------------------------------------------------------- status ------
def list_queue(folder):
    out = []
    root = os.path.join(WIKI_DIR, folder)
    for dirpath, _, files in os.walk(root):
        for fn in sorted(files):
            if fn.startswith(".") or fn.startswith("Icon") or fn.startswith("~$") or fn == "README.md":
                continue
            if fn.lower().endswith(PARTIAL_EXT):
                continue
            out.append(os.path.relpath(os.path.join(dirpath, fn), WIKI_DIR).replace(os.sep, "/"))
    return sorted(out)


def runner_state():
    try:
        pid = int(open(os.path.join(LOCK_DIR, "pid")).read().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return {"running": False}
    started = ""
    try:
        started = open(os.path.join(LOCK_DIR, "started")).read().strip()
    except OSError:
        pass
    return {"running": True, "since": started}


def status():
    cfg = load_config()
    try:
        version = open(os.path.join(WIKI_DIR, ".wiki-engine", "VERSION")).read().strip()
    except OSError:
        version = ""
    return {
        "title": cfg.get("title") or "Wiki",
        "engine": cfg.get("engine") or "claude",
        "version": version,
        "maxUploadMb": round(max_upload_bytes() / 1024 / 1024),
        "queue": {k: list_queue(v) for k, v in QUEUES.items()},
        "needsReview": list_queue("raw/_needs-review"),
        "runner": runner_state(),
        "now": wiki_events.read_now(),
    }


def is_inline(ctype):
    return ctype.split(";")[0].strip().lower() in INLINE_TYPES


def start_runner():
    """Start the runner now. Under launchd the viewer's label names the runner's."""
    label = os.environ.get("XPC_SERVICE_NAME", "")
    if label.endswith(".viewer"):
        runner = label[: -len(".viewer")] + ".runner"
        try:
            r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{runner}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            if r.returncode == 0:
                return "started"
        except (OSError, subprocess.SubprocessError):
            pass
    subprocess.Popen(["/bin/bash", os.path.join(WIKI_DIR, "scripts", "wiki_runner.sh")],
                     cwd=WIKI_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return "started"


# -------------------------------------------------------------------- events -------
def history_offset(path, lines):
    """Byte offset where the last `lines` lines of the log begin."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    start = max(0, size - 1024 * 1024)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    seen = 0
    for i in range(len(data) - 2, -1, -1):
        if data[i] == 10:
            seen += 1
            if seen == lines:
                return start + i + 1
    if start == 0:
        return 0
    return start + data.find(b"\n") + 1  # skip the partial first line


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "wiki"
    sys_version = ""

    def log_message(self, fmt, *args):  # keep the LaunchAgent log quiet
        pass

    # ------------------------------------------------------------ plumbing ----
    def _headers(self, status, ctype, length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send_bytes(self, status, ctype, body, head_only, extra=None):
        self._headers(status, ctype, len(body), extra)
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self._send_bytes(status, "application/json; charset=utf-8", body, False,
                         {"X-Frame-Options": "DENY"})

    def _forbid(self, head_only=False, why="Forbidden"):
        self._send_bytes(403, "text/plain; charset=utf-8", (why + "\n").encode(), head_only)

    def _send_file(self, path, head_only, status=200, original=False):
        ext = os.path.splitext(path)[1].lower()
        ctype = EXTRA_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        size = os.path.getsize(path)
        extra = None
        if original:
            # A source document is content, never code: no script runs from it here.
            extra = {}
            if ctype.split(";")[0] != "application/pdf":  # Chrome's PDF viewer needs no sandbox
                extra["Content-Security-Policy"] = "sandbox"
            if not is_inline(ctype):
                name = urllib.parse.quote(os.path.basename(path))
                extra["Content-Disposition"] = f"attachment; filename*=UTF-8''{name}"
        self._headers(status, ctype, size, extra)
        if head_only:
            return
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _not_found(self, head_only):
        page = safe_join(os.path.join(WIKI_DIR, "public"), "404.html")
        if page and os.path.isfile(page):
            return self._send_file(page, head_only, status=404)
        self._send_bytes(404, "text/plain; charset=utf-8", b"Not found\n", head_only)

    # -------------------------------------------------------------- guards ----
    def _own_origins(self):
        port = self.server.server_address[1]
        return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _host_ok(self):
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "").lower() in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _origin_for(self, port):
        """Same host name the browser used (127.0.0.1 or localhost), another port."""
        host = (self.headers.get("Host") or "127.0.0.1").rsplit(":", 1)[0].lower()
        return f"http://{host if host in ('127.0.0.1', 'localhost') else '127.0.0.1'}:{port}"

    def _redirect(self, url, head_only):
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _token_ok(self, query):
        given = self.headers.get("X-Wiki-Token") or (query.get("token") or [""])[0]
        return hmac.compare_digest(given.encode(), TOKEN.encode())

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        # Browsers always send Origin on a POST; tools on this Mac (curl) may not.
        return origin is None or origin in self._own_origins()

    # ---------------------------------------------------------------- GET -----
    def _resolve_site(self, rel):
        public = os.path.join(WIKI_DIR, "public")
        if not os.path.isdir(public):
            return "NOT_BUILT"
        candidates = []
        if rel == "" or rel.endswith("/"):
            candidates.append(rel + "index.html")
        else:
            candidates += [rel, rel + ".html", rel + "/index.html"]
        for c in candidates:
            p = safe_join(public, c)
            if p and os.path.isfile(p):
                return p
        return None

    def _upload_page(self, head_only):
        try:
            html = open(UPLOAD_PAGE, encoding="utf-8").read()
        except OSError:
            return self._not_found(head_only)
        cfg = load_config()
        boot = json.dumps({"token": TOKEN, "title": cfg.get("title") or "Wiki",
                           "site": self._origin_for(self.server.site_port)})
        html = html.replace("__WIKI_BOOT__", boot.replace("</", "<\\/"))
        self._send_bytes(200, "text/html; charset=utf-8", html.encode("utf-8"), head_only,
                         {"X-Frame-Options": "DENY",
                          "Content-Security-Policy": "frame-ancestors 'none'"})

    def _handle(self, head_only):
        if not self._host_ok():
            return self._forbid(head_only, "Forbidden: unknown host")
        split = urllib.parse.urlsplit(self.path)
        rel = urllib.parse.unquote(split.path).lstrip("/")
        query = urllib.parse.parse_qs(split.query)
        # Normalise without letting ".." climb (safe_join rejects it anyway).
        if rel and posixpath.normpath(rel).startswith(".."):
            return self._forbid(head_only)
        if self.server.role == "app":
            if rel in ("upload", "upload/"):
                return self._upload_page(head_only)
            if rel.startswith("api/"):
                if not self._token_ok(query):
                    return self._forbid(head_only, "Forbidden: open the Upload page from the wiki")
                if rel == "api/status":
                    return self._send_json(200, status())
                if rel == "api/events" and not head_only:
                    return self._stream_events(query)
                return self._send_bytes(404, "text/plain; charset=utf-8", b"Not found\n", head_only)
            # Anything else belongs to the site.
            return self._redirect(self._origin_for(self.server.site_port) + self.path, head_only)
        if rel in ("upload", "upload/"):
            if not self.server.app_running:
                return self._send_bytes(503, "text/plain; charset=utf-8",
                                        f"The Upload page could not start: port {self.server.app_port} is in "
                                        "use by another program. Set \"uploadPort\" in wiki.config.json to a "
                                        "free port and restart the Mac.\n".encode(), head_only)
            return self._redirect(self._origin_for(self.server.app_port) + "/upload", head_only)
        top = rel.split("/", 1)[0]
        if top in ROOTS:
            sub = rel.split("/", 1)[1] if "/" in rel else ""
            p = safe_join(os.path.join(WIKI_DIR, ROOTS[top]), sub)
            if p is None:
                return self._forbid(head_only)
            if os.path.isfile(p):
                return self._send_file(p, head_only, original=True)
            return self._not_found(head_only)
        p = self._resolve_site(rel)
        if p == "NOT_BUILT":
            return self._send_bytes(503, "text/html; charset=utf-8", NOT_BUILT.encode(), head_only)
        if p is None:
            return self._not_found(head_only)
        return self._send_file(p, head_only)

    def _stream_events(self, query):
        """Server-sent events: recent history, then new lines as they are written."""
        path = wiki_events.EVENTS
        since = self.headers.get("Last-Event-ID") or (query.get("since") or [""])[0]
        try:
            offset = int(since)
        except ValueError:
            offset = history_offset(path, HISTORY_LINES)
        self._headers(200, "text/event-stream; charset=utf-8", None, {"X-Frame-Options": "DENY"})
        self.wfile.write(b"retry: 2000\n\n")
        self.wfile.flush()
        last_beat, buf, now_seen = time.time(), b"", None
        while True:
            # What is happening right now: sent whenever it changes. No id line, so a
            # reconnect still resumes the log where it left off.
            try:
                st = os.stat(wiki_events.NOW)
                stamp = (st.st_mtime_ns, st.st_size)
            except OSError:
                stamp = None
            if stamp and stamp != now_seen:
                now_seen = stamp
                rec = wiki_events.read_now()
                if rec:
                    self.wfile.write(b"event: now\ndata: " + json.dumps(rec, ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size < offset:  # the log rotated
                offset, buf = 0, b""
            if size > offset:
                with open(path, "rb") as f:
                    f.seek(offset)
                    data = f.read(size - offset)
                offset += len(data)
                buf += data
                lines = buf.split(b"\n")
                buf = lines.pop()
                # One SSE message per event line; the last carries the resume offset.
                lines = [ln for ln in lines if ln.strip()]
                end = offset - len(buf)
                if lines:
                    out = [b"data: " + ln + b"\n\n" for ln in lines]
                    out[-1] = b"id: " + str(end).encode() + b"\n" + out[-1]
                    self.wfile.write(b"".join(out))
                    self.wfile.flush()
                    last_beat = time.time()
            if time.time() - last_beat >= HEARTBEAT_SECONDS:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                last_beat = time.time()
            time.sleep(0.5)

    # --------------------------------------------------------------- POST -----
    def _handle_post(self):
        if not self._host_ok():
            return self._forbid(why="Forbidden: unknown host")
        if self.server.role != "app":
            return self._send_bytes(404, "text/plain; charset=utf-8", b"Not found\n", False)
        split = urllib.parse.urlsplit(self.path)
        rel = urllib.parse.unquote(split.path).lstrip("/")
        query = urllib.parse.parse_qs(split.query)
        if not self._origin_ok() or not self._token_ok(query):
            return self._forbid(why="Forbidden: open the Upload page from the wiki")
        if rel == "api/upload":
            return self._upload(query)
        if rel == "api/process":
            wiki_events.emit("process-requested")
            return self._send_json(200, {"ok": True, "runner": start_runner()})
        self._send_bytes(404, "text/plain; charset=utf-8", b"Not found\n", False)

    def _upload(self, query):
        name, why = clean_upload_name((query.get("name") or [""])[0])
        if name is None:
            self._drain()
            return self._send_json(200, {"ok": False, "skipped": why})
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            return self._send_json(411, {"ok": False, "error": "the upload had no length"})
        limit = max_upload_bytes()
        if length > limit:
            self.close_connection = True
            return self._send_json(413, {"ok": False, "error":
                                         f"larger than the {round(limit / 1024 / 1024)} MB limit"})
        os.makedirs(UPLOAD_TMP, exist_ok=True)
        tmp = os.path.join(UPLOAD_TMP, f".upload-{secrets.token_hex(8)}.part")
        try:
            left = length
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise ConnectionError("upload interrupted")
                    f.write(chunk)
                    left -= len(chunk)
            queue, dest, duplicate = place_upload(tmp, name)
        except (OSError, ConnectionError) as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if isinstance(e, ConnectionError):
                self.close_connection = True
                return
            return self._send_json(500, {"ok": False, "error": "could not save the file"})
        path = os.path.relpath(dest, WIKI_DIR).replace(os.sep, "/")
        wiki_events.emit("upload", file=path, name=name, queue=queue, bytes=length,
                         duplicate=True if duplicate else None)
        return self._send_json(200, {"ok": True, "file": path, "queue": queue,
                                     "duplicate": duplicate})

    def _drain(self):
        try:
            left = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            left = 0
        if left > max_upload_bytes():
            self.close_connection = True
            return
        while left > 0:
            chunk = self.rfile.read(min(1 << 20, left))
            if not chunk:
                break
            left -= len(chunk)

    # ------------------------------------------------------------- verbs ------
    def do_GET(self):
        try:
            self._handle(head_only=False)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        try:
            self._handle(head_only=True)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        try:
            self._handle_post()
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    app_running = True

    def __init__(self, addr, role, site_port, app_port):
        self.role, self.site_port, self.app_port = role, site_port, app_port
        super().__init__(addr, Handler)

    def server_bind(self):
        # HTTPServer.server_bind looks the address up by name (socket.getfqdn), a network
        # lookup this server never needs. Bind, and name it by its address instead.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = BIND, self.server_address[1]


def main():
    ap = argparse.ArgumentParser(description="Serve the wiki to this Mac only.")
    ap.add_argument("--port", type=int, default=default_port())
    ap.add_argument("--upload-port", type=int, default=None)
    a = ap.parse_args()
    wiki_netguard.install()
    app_port = a.upload_port or default_upload_port(a.port)
    # Nothing can be mid-upload when the viewer starts: leftovers are from a killed one,
    # and would make every run wait for "files still copying".
    if os.path.isdir(UPLOAD_TMP):
        for name in os.listdir(UPLOAD_TMP):
            try:
                os.unlink(os.path.join(UPLOAD_TMP, name))
            except OSError:
                pass
    site = Server((BIND, a.port), "site", a.port, app_port)
    try:
        app = Server((BIND, app_port), "app", a.port, app_port)
        threading.Thread(target=app.serve_forever, daemon=True).start()
    except OSError as e:  # the wiki stays readable even if the Upload port is taken
        site.app_running = False
        print(f"wiki: the Upload page could not start on port {app_port}: {e}", file=sys.stderr, flush=True)
    print(f"wiki: serving {WIKI_DIR} at http://{BIND}:{a.port}/ "
          f"(Upload at http://{BIND}:{app_port}/upload)", file=sys.stderr, flush=True)
    site.serve_forever()


if __name__ == "__main__":
    main()
