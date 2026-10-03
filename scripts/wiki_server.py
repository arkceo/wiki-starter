#!/usr/bin/env python3
"""wiki_server.py — serve the wiki to this Mac, and only to this Mac.

Run by a LaunchAgent with KeepAlive. Two origins, on two ports of 127.0.0.1:

  the site (port, default 8765)
    /                  the built site in public/ (Quartz clean URLs: /page -> page.html,
                       /folder/ -> folder/index.html)
    /raw/<path>        source documents, read-only, so "Sources" links open the original
    /archive/<path>    processed Update Packets, read-only
    /upload            redirects to the Upload app
    /.wiki/build       which build of the site is being served; every page carries a small
                       script that asks, and shows a new build without a manual refresh
  the Upload app (uploadPort, default port + 1)
    /upload            the Upload & Logs page (engine/upload/upload.html)
    /api/upload        POST one file into the queue (raw/_intake/, or raw/inbox/ for a packet)
    /api/events        the activity log as a live stream (server-sent events), and what
                       is happening right now as `now` events
    /api/status        what is queued, running and waiting for review, and how long and
                       how much the documents still waiting should take (wiki_estimate.py)
    /api/process       POST: start the runner now
    /api/review        the review questions (scripts/wiki_review.py); POST
                       /api/review/answer settles one, /api/review/score asks Jev again
    /api/settings      GET, or POST to change, the settings in wiki.config.json the owner may
                       change here; POST /api/settings/jev-key saves the TypeSafe key in the
                       Keychain (or removes it)

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
local network access. The one outside call: with Claude reading and a TypeSafe key saved,
review questions go to TypeSafe's Jev to be scored (scripts/wiki_jev.py). Standard
library only.

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
import wiki_estimate  # noqa: E402
import wiki_events  # noqa: E402
import wiki_jev  # noqa: E402
import wiki_netguard  # noqa: E402
import wiki_review  # noqa: E402

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
    "This page opens the wiki by itself when it is ready.</p></body>"
)
# Added to every page of the site: when a new build is served (pages written while a long
# upload is still being read), a page nobody is reading reloads itself; one being read
# offers a button instead, so nobody loses their place.
LIVE_SCRIPT = (
    "<script>(()=>{if(window.__wikiLive)return;window.__wikiLive=1;let first=null,"
    "pages=0,touched=0;const mark=()=>{touched=Date.now()};for(const e of"
    "[\"scroll\",\"keydown\",\"pointerdown\",\"wheel\"])addEventListener(e,mark,{passive:true});"
    "function show(n){let b=document.getElementById(\"wiki-live\");if(!b){b=document.createElement"
    "(\"button\");b.id=\"wiki-live\";b.type=\"button\";b.onclick=()=>location.reload();b.style.cssText="
    "\"position:fixed;right:16px;bottom:16px;z-index:2147483647;font:600 14px -apple-system,"
    "sans-serif;padding:10px 16px;border-radius:999px;border:0;background:#284b63;color:#fff;"
    "box-shadow:0 2px 10px rgba(0,0,0,.25);cursor:pointer\";document.body.append(b)}"
    "b.textContent=\"The wiki was updated\"+(n>0?\" \\u00b7 \"+n+\" new page\"+(n===1?\"\":\"s\"):\"\")"
    "+\" \\u00b7 Show\"}async function check(){try{const r=await fetch(\"/.wiki/build\","
    "{cache:\"no-store\"});if(!r.ok)return;const s=await r.json();if(first===null){first=s.stamp;"
    "pages=s.pages;return}if(s.stamp===first)return;const quiet=Date.now()-touched>20000&&"
    "scrollY<40&&!document.querySelector(\"input:focus,textarea:focus,[contenteditable]:focus\");"
    "if(quiet||!first){location.reload();return}show(s.pages-pages)}catch(e){}}"
    "setInterval(check,5000);check()})()</script>"
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


# Time left and cost, from the batches measured so far (scripts/wiki_estimate.py). The
# tracker reads only what the event log gained since the last poll.
ESTIMATE = wiki_estimate.Tracker()


def estimate(cfg, queue, runner):
    try:
        ESTIMATE.read([os.path.join(STATE_DIR, "events.1.jsonl"), wiki_events.EVENTS])
        fast = cfg.get("engine") != "local" and cfg.get("pipeline") != "classic"
        fast_cfg = cfg.get("fast") if isinstance(cfg.get("fast"), dict) else {}
        return ESTIMATE.estimate(len(queue["intake"]), cfg.get("engine"),
                                 1 if fast else cfg.get("parallelBatches", 3),   # fast: one batch at a time
                                 (fast_cfg.get("docsPerBatch") or 40) if fast else cfg.get("docsPerBatch"),
                                 runner.get("running", False), fast=fast)
    except Exception as e:  # an estimate is never worth breaking the status for
        print(f"estimate: {e}", file=sys.stderr)
        return {"ready": False}


def status():
    cfg = load_config()
    try:
        version = open(os.path.join(WIKI_DIR, ".wiki-engine", "VERSION")).read().strip()
    except OSError:
        version = ""
    queue = {k: list_queue(v) for k, v in QUEUES.items()}
    runner = runner_state()
    return {
        "title": cfg.get("title") or "Wiki",
        "engine": cfg.get("engine") or "claude",
        "version": version,
        "maxUploadMb": round(max_upload_bytes() / 1024 / 1024),
        "queue": queue,
        "needsReview": list_queue("raw/_needs-review"),
        "review": {"open": wiki_review.count_open(), "rev": wiki_review.revision()},
        "runner": runner,
        "now": wiki_events.read_now(),
        "estimate": estimate(cfg, queue, runner),
    }


def is_inline(ctype):
    return ctype.split(";")[0].strip().lower() in INLINE_TYPES


_build = {"key": None, "info": {"stamp": "", "pages": 0}}


def build_info():
    """Which build of the site is served. The runner swaps public/ in one step, so the
    folder's identity and its index page's time change together."""
    public = os.path.join(WIKI_DIR, "public")
    try:
        key = (os.stat(public).st_ino, os.stat(os.path.join(public, "index.html")).st_mtime_ns)
    except OSError:
        return {"stamp": "", "pages": 0}
    if key != _build["key"]:
        pages = 0
        for _, _, files in os.walk(public):
            pages += sum(1 for f in files if f.endswith(".html") and f != "404.html")
        _build.update(key=key, info={"stamp": f"{key[0]}-{key[1]}", "pages": pages})
    return _build["info"]


def with_live_script(html):
    i = html.rfind(b"</body>")
    return html[:i] + LIVE_SCRIPT.encode() + html[i:] if i >= 0 else html + LIVE_SCRIPT.encode()


# ------------------------------------------------------------------- settings ------
CONFIG_LOCK = threading.Lock()
# What the Settings panel may change: key -> (type, low, high). Everything else in
# wiki.config.json (engine, port, the local model) stays as the installer wrote it.
SETTABLE = {"parallelBatches": (int, 1, 6), "docsPerBatch": (int, 1, 50),
            "maxUploadMb": (int, 1, 4096), "maxSpendPerBatchUsd": (float, 0.5, 100.0)}
LABELS = {"parallelBatches": "batches read at once", "docsPerBatch": "documents per batch",
          "maxUploadMb": "the upload limit in MB", "maxSpendPerBatchUsd": "the spending cap per batch in USD"}


def number(v, kind, default):
    try:
        return kind(v)
    except (TypeError, ValueError):
        return default


def settings_view():
    cfg = load_config()
    engine = cfg.get("engine") or "claude"
    spend = cfg.get("maxSpendPerBatchUsd", cfg.get("maxSpendPerRunUsd", 5))
    fast = engine != "local" and cfg.get("pipeline") != "classic"   # Fast is the default
    fast_docs = number((cfg.get("fast") or {}).get("docsPerBatch") if isinstance(cfg.get("fast"), dict) else None, int, 0)
    return {
        "engine": engine,
        "title": cfg.get("title") or "",
        "company": cfg.get("company") or "",
        "parallelBatches": max(1, min(6, number(cfg.get("parallelBatches"), int, 3))),
        "pipeline": "classic" if cfg.get("pipeline") == "classic" else "fast",
        # Fast reading has batches of its own size (fast.docsPerBatch); the field edits that one.
        "docsPerBatch": (fast_docs or 40) if fast else (number(cfg.get("docsPerBatch"), int, 0)
                                                        or (20 if engine == "local" else 8)),
        "maxUploadMb": round(max_upload_bytes() / 1024 / 1024),
        "maxSpendPerBatchUsd": number(spend, float, 5.0),
        "review": wiki_jev.review_settings(cfg),
        "jev": {"available": wiki_jev.available(cfg), "key": bool(KEY.get())},
    }


def clean_text(v, limit):
    v = "".join(ch for ch in str(v) if ch >= " " and ch != "\x7f").strip()
    return v[:limit]


def change_settings(body):
    """Apply the owner's changes to wiki.config.json. Returns (changed keys, errors)."""
    if not isinstance(body, dict):
        return [], ["the settings were not sent as an object"]
    errors, updates = [], {}
    if "title" in body:
        t = clean_text(body["title"], 80)
        if t:
            updates["title"] = t
        else:
            errors.append("the title cannot be empty")
    if "company" in body:
        updates["company"] = clean_text(body["company"], 120)
    if "pipeline" in body:
        if body["pipeline"] in ("classic", "fast"):
            updates["pipeline"] = body["pipeline"]
        else:
            errors.append("how Claude reads must be classic or fast")
    for k, (kind, lo, hi) in SETTABLE.items():
        if k in body:
            v = number(body[k], kind, None)
            if v is None or v != v or not lo <= v <= hi:
                errors.append(f"{LABELS[k]} must be between {lo:g} and {hi:g}")
            else:
                updates[k] = round(v, 2) if kind is float else v
    if isinstance(body.get("review"), dict):
        r = dict(wiki_jev.review_settings())
        mode = body["review"].get("mode", r["mode"])
        bar = number(body["review"].get("autoConfidence", r["autoConfidence"]), float, None)
        if mode not in ("auto", "manual"):
            errors.append("the review mode must be auto or manual")
        elif bar is None or not 0.5 <= bar <= 0.99:
            errors.append("the auto-review confidence must be between 50% and 99%")
        else:
            updates["review"] = {"mode": mode, "autoConfidence": round(bar, 2)}
    if errors:
        return [], errors
    path = os.path.join(WIKI_DIR, "wiki.config.json")
    with CONFIG_LOCK:
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return [], ["wiki.config.json could not be read; fix or restore it first"]
        pipeline = updates.get("pipeline", cfg.get("pipeline")) or "fast"
        if "docsPerBatch" in updates and cfg.get("engine") != "local" and pipeline == "fast":
            # Fast reading's batches are its own (fast.docsPerBatch), not the classic size.
            fast_cfg = dict(cfg.get("fast") or {}) if isinstance(cfg.get("fast"), dict) else {}
            if fast_cfg.get("docsPerBatch") != updates["docsPerBatch"]:
                fast_cfg["docsPerBatch"] = updates["docsPerBatch"]
                updates["fast"] = fast_cfg
            del updates["docsPerBatch"]
        changed = [k for k, v in updates.items() if cfg.get(k) != v]
        if changed:
            cfg.update(updates)
            if "maxSpendPerBatchUsd" in changed:
                cfg.pop("maxSpendPerRunUsd", None)   # the older name, now replaced
            tmp = path + f".{secrets.token_hex(4)}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
    return changed, []


class KeyCache:
    """The TypeSafe key, read from the Keychain at most once a minute."""

    def __init__(self):
        self.value, self.at, self.lock = "", 0.0, threading.Lock()

    def get(self):
        with self.lock:
            if time.time() - self.at > 60:
                self.value, self.at = wiki_jev.get_key(), time.time()
            return self.value

    def forget(self):
        with self.lock:
            self.at = 0.0


KEY = KeyCache()


class ReviewWorker(threading.Thread):
    """Scores new review questions with Jev as soon as they appear, and in auto mode
    applies the answers it is sure enough of."""
    daemon = True

    def __init__(self):
        super().__init__(name="review")
        self.wake, self.force = threading.Event(), set()
        self.busy, self.error = False, ""

    def nudge(self, ids=()):
        self.force |= set(ids)
        self.wake.set()

    def run(self):
        while True:
            self.wake.wait(5)
            self.wake.clear()
            try:
                self.step()
            except Exception as e:  # never let one bad item stop the worker
                self.busy = False
                wiki_events.log(f"review worker: {e!r}")

    def step(self):
        if not wiki_jev.available():
            return
        items = wiki_review.list_open()
        rs = wiki_jev.review_settings()
        force, self.force = self.force, set()
        due = [i for i in items if wiki_jev.needs_score(i, i["id"] in force)]
        ready = rs["mode"] == "auto" and any(wiki_jev.auto_answer(i, rs["autoConfidence"]) for i in items)
        key = KEY.get() if due else ""
        if not (due and key) and not ready:
            return
        self.busy = True
        summary = wiki_jev.run(force_ids=force, key=key)
        self.busy = False
        if due and key:
            self.error = summary["error"]
        if summary["queued"]:
            start_runner()


WORKER = ReviewWorker()


def review_view():
    cfg = load_config()
    return {
        "items": wiki_review.list_open(),
        "done": wiki_review.list_done(30),
        "engine": cfg.get("engine") or "claude",
        "review": wiki_jev.review_settings(cfg),
        "jev": {"available": wiki_jev.available(cfg), "key": bool(KEY.get()),
                "busy": WORKER.busy, "error": WORKER.error},
        "rev": wiki_review.revision(),
    }


def rebuild_site():
    """Rebuild the site in the background (the title is built into its pages). A run in
    progress rebuilds at its end anyway; this one then exits at once."""
    subprocess.Popen(["/bin/bash", os.path.join(WIKI_DIR, "scripts", "wiki_runner.sh"), "--rebuild"],
                     cwd=WIKI_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


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
        if not original and ext == ".html":
            with open(path, "rb") as f:
                return self._send_bytes(status, ctype, with_live_script(f.read()), head_only)
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
                if rel == "api/review":
                    return self._send_json(200, review_view())
                if rel == "api/settings":
                    return self._send_json(200, settings_view())
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
        if rel == ".wiki/build":
            return self._send_json(200, build_info())
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
            return self._send_bytes(503, "text/html; charset=utf-8", with_live_script(NOT_BUILT.encode()),
                                    head_only)
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
        if rel in ("api/review/answer", "api/review/score", "api/settings", "api/settings/jev-key"):
            body = self._read_json()
            if body is None:
                return self._send_json(400, {"ok": False, "message": "The request was not valid JSON."})
            return getattr(self, "_" + rel[4:].replace("/", "_").replace("-", "_"))(body)
        self._send_bytes(404, "text/plain; charset=utf-8", b"Not found\n", False)

    def _read_json(self, limit=65536):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length > limit:
            self.close_connection = True
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def _review_answer(self, body):
        res = wiki_review.answer(str(body.get("id") or ""), key=body.get("key") or "",
                                 text=str(body.get("text") or "")[:2000], by="owner")
        if res.get("queued"):
            start_runner()
        return self._send_json(200 if res["ok"] else 409, res)

    def _review_score(self, body):
        if not wiki_jev.available():
            return self._send_json(409, {"ok": False, "message":
                                         "This wiki reads with the model on this Mac, so nothing is sent to Jev."})
        if not KEY.get():
            return self._send_json(409, {"ok": False, "message": "Add a TypeSafe API key in Settings first."})
        ids = body.get("ids")
        ids = [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else \
            [i["id"] for i in wiki_review.list_open()]
        WORKER.error = ""
        WORKER.nudge(ids)
        return self._send_json(200, {"ok": True, "message": f"Asking Jev about {len(ids)} question(s)."})

    def _settings(self, body):
        changed, errors = change_settings(body)
        if errors:
            msg = "; ".join(errors)
            return self._send_json(400, {"ok": False, "message": msg[0].upper() + msg[1:] + "."})
        if "title" in changed and not runner_state()["running"]:
            rebuild_site()   # a run in progress rebuilds at its end anyway
        if changed:
            wiki_events.emit("settings", msg="Settings changed: " + ", ".join(changed))
        WORKER.nudge()
        return self._send_json(200, {"ok": True, "changed": changed, "settings": settings_view()})

    def _settings_jev_key(self, body):
        key = "".join(str(body.get("key") or "").split())
        if not key:
            wiki_jev.delete_key()
            KEY.forget()
            wiki_events.emit("settings", msg="The TypeSafe API key was removed")
            return self._send_json(200, {"ok": True, "message": "The key was removed.",
                                         "settings": settings_view()})
        if len(key) > 512 or not key.isprintable():
            return self._send_json(400, {"ok": False, "message": "That does not look like an API key."})
        ok, message = wiki_jev.test_key(key)
        if not ok and "did not accept" in message:
            return self._send_json(400, {"ok": False, "message": message[0].upper() + message[1:] + "."})
        if not wiki_jev.set_key(key):
            return self._send_json(500, {"ok": False, "message": "The key could not be saved in the Keychain."})
        KEY.forget()
        WORKER.nudge()
        wiki_events.emit("settings", msg="A TypeSafe API key was saved in the Keychain")
        message = ("Saved in the Keychain. " + message) if ok else \
            f"Saved in the Keychain, but it could not be checked just now ({message})."
        return self._send_json(200, {"ok": True, "message": message, "settings": settings_view()})

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
        WORKER.start()
    except OSError as e:  # the wiki stays readable even if the Upload port is taken
        site.app_running = False
        print(f"wiki: the Upload page could not start on port {app_port}: {e}", file=sys.stderr, flush=True)
    print(f"wiki: serving {WIKI_DIR} at http://{BIND}:{a.port}/ "
          f"(Upload at http://{BIND}:{app_port}/upload)", file=sys.stderr, flush=True)
    site.serve_forever()


if __name__ == "__main__":
    main()
