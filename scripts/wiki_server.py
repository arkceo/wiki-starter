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
    /api/upload/direct POST, on a server with an upload helper (WIKI_UPLOAD_HELPER, set by the
                       cloud session in cloud/session.py): an address the browser sends one
                       file to straight, in object storage, instead of through a proxy that
                       caps request sizes; POST /api/upload/direct/done then brings it into
                       the queue exactly as /api/upload would
    /api/events        the activity log as a live stream (server-sent events), and what
                       is happening right now as `now` events
    /api/status        what is queued, running (and for how long nothing has moved) and
                       waiting for review or action, and how long and how much the
                       documents still waiting should take (wiki_estimate.py)
    /api/process       POST: start the runner now
    /api/restart       POST: stop a run that is stuck, with everything it started, and
                       start a fresh one
    /api/review        the review questions (scripts/wiki_review.py), each with where its
                       document is now and the page citing it; POST /api/review/answer
                       settles one, /api/review/reopen opens an answered one again to change
                       the answer, /api/review/score asks Jev again
    /api/actions       the suggested actions (scripts/wiki_actions.py); POST
                       /api/actions/update assigns one, or marks it done, dismissed or open
    /api/ask           the last questions asked of the wiki and their answers; POST asks
                       one (scripts/wiki_ask.py, run as its own process: it reads the pages
                       here and asks Claude), POST /api/ask/save files an answer in the
                       wiki and /api/ask/correct sends the owner's correction, each as an
                       Update Packet
    /api/settings      GET, or POST to change, the settings in wiki.config.json the owner may
                       change here: the performance mode and the figures it sets
                       (scripts/wiki_settings.py), reading speed, title, company, reading
                       and review; POST /api/settings/jev-key saves the TypeSafe key in the
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
import copy
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
import shutil
import signal
import socketserver
import subprocess
import sys
import time
import urllib.parse
import urllib.request

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WIKI_DIR, "scripts"))
import wiki_actions  # noqa: E402
import wiki_ask  # noqa: E402
import wiki_estimate  # noqa: E402
import wiki_events  # noqa: E402
import wiki_jev  # noqa: E402
import wiki_netguard  # noqa: E402
import wiki_review  # noqa: E402
import wiki_settings  # noqa: E402

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
    "+\" \\u00b7 Show\"}async function check(){try{const r=await fetch(\"@SITE_BASE@/.wiki/build\","
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
# Behind a proxy that serves the wiki under paths of its own site (Nucleus Cloud), where the
# site and the Upload app are, as the browser sees them: e.g. "/wiki" and "/wiki/_app".
# Empty on a Mac, where each is its own origin on 127.0.0.1.
SITE_BASE = (os.environ.get("WIKI_SITE_BASE") or "").rstrip("/")
APP_BASE = (os.environ.get("WIKI_APP_BASE") or "").rstrip("/")
HISTORY_LINES = 800
# A server deployment (cloud/session.py) runs a helper on 127.0.0.1 that hands out storage
# addresses for uploads and brings the files back down; empty on a Mac.
UPLOAD_HELPER = (os.environ.get("WIKI_UPLOAD_HELPER") or "").rstrip("/")
HEARTBEAT_SECONDS = 15


def load_config():
    return wiki_settings.load(os.path.join(WIKI_DIR, "wiki.config.json"))


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
    """The largest single upload: set by the performance mode, or by hand."""
    return int(wiki_settings.effective(load_config())["maxUploadMb"] * 1024 * 1024)


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


def upload_helper(path, body):
    """One call to the upload helper on 127.0.0.1 (never through a proxy)."""
    req = urllib.request.Request(UPLOAD_HELPER + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=900) as r:
        return json.loads(r.read() or b"{}")


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


def lock_pid():
    """The pid in the runner's lock, or None."""
    try:
        pid = int(open(os.path.join(LOCK_DIR, "pid")).read().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 1 else None


def is_runner(pid):
    """Whether pid is a runner that is still running. A lock left by a runner that was
    killed holds a pid some other program may have by now (pids are reused): that one
    must not show "Working" forever, so the process's command line must name the runner."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass   # alive, but not ours to signal: ps says what it is
    except OSError:
        return False
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True   # without ps, alive is all that can be told
    return "wiki_runner.sh" in r.stdout


# Touched by the run whenever it makes real progress (an event, or a new step on the live
# line); the live line itself is rewritten every half minute while documents are being
# read, so its own time says the run is alive, not that it is getting anywhere.
PROGRESS = os.path.join(STATE_DIR, "progress")
# Conversion running ahead of the reading keeps its own marker (the runner's watchdog
# watches the reading alone); for "is the run getting anywhere" either counts.
CONVERT_PROGRESS = os.path.join(STATE_DIR, "convert-progress")


def runner_state():
    """{"running": False}, or {"running": True, "since": "HH:MM", "stalledFor": seconds since
    the run last made progress (or started): a run that gets nowhere for long is stuck}."""
    pid = lock_pid()
    if pid is None or not is_runner(pid):
        return {"running": False}
    started = ""
    try:
        started = open(os.path.join(LOCK_DIR, "started")).read().strip()
    except OSError:
        pass
    marks = []
    for p in (PROGRESS, CONVERT_PROGRESS, LOCK_DIR) if os.path.exists(PROGRESS) else (wiki_events.NOW, LOCK_DIR):
        try:
            marks.append(os.path.getmtime(p))
        except OSError:
            pass
    stalled = max(0, int(time.time() - max(marks))) if marks else 0
    return {"running": True, "since": started, "stalledFor": stalled}


# ------------------------------------------------------------------ restart --------
def process_table():
    """{pid: (parent pid, state)} for every process on this Mac (ps -A)."""
    try:
        r = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,stat="], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in r.stdout.splitlines():
        f = line.split()
        if len(f) >= 2 and f[0].isdigit() and f[1].isdigit():
            table[int(f[0])] = (int(f[1]), f[2] if len(f) > 2 else "")
    return table


def process_tree(root, table=None):
    """root and every process it started, and they started, found by their parent pids."""
    table = process_table() if table is None else table
    kids = {}
    for pid, (ppid, _) in table.items():
        kids.setdefault(ppid, []).append(pid)
    out, todo = [], [root]
    while todo:
        p = todo.pop()
        if p in out or p <= 1:
            continue
        out.append(p)
        todo += kids.get(p, [])
    return out


def still_alive(pids):
    """Those of pids still running (a finished process its parent has not collected yet,
    a zombie, counts as gone)."""
    table = process_table()
    return [p for p in pids if p in table and not table[p][1].startswith("Z")]


def signal_all(pids, sig):
    for p in pids:
        try:
            os.kill(p, sig)
        except OSError:
            pass


def stop_runner(pid, grace=5.0):
    """Stop the runner and everything it started: SIGTERM to the whole tree, then SIGKILL
    to whatever is left after `grace` seconds. Returns how many processes it stopped."""
    pids = process_tree(pid)
    signal_all(pids, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline and still_alive(pids):
        time.sleep(0.2)
    left = still_alive(pids)
    if left:
        # What is left, and anything it started while it was stopping.
        left = sorted(set(left) | set(q for p in left for q in process_tree(p)))
        signal_all(left, signal.SIGKILL)
        deadline = time.time() + 2
        while time.time() < deadline and still_alive(left):
            time.sleep(0.1)
    return len(pids)


def stale_lock():
    """Whether runner.lock is left over from a runner that is gone. A lock with no pid yet
    is a runner starting this instant, unless it is old."""
    if not os.path.isdir(LOCK_DIR):
        return False
    pid = lock_pid()
    if pid is not None:
        return not is_runner(pid)
    try:
        return time.time() - os.path.getmtime(LOCK_DIR) > 10
    except OSError:
        return False


def restart():
    """Stop a run (stuck, or just running) and start a fresh one. Returns the message."""
    pid = lock_pid()
    running = pid is not None and is_runner(pid)
    if running:
        stop_runner(pid)
    cleared = not running and stale_lock()
    if running or cleared:
        shutil.rmtree(LOCK_DIR, ignore_errors=True)
        wiki_events.set_now("idle")
    if running:
        wiki_events.emit("restart", msg="Processing was restarted from the Upload page")
    else:
        wiki_events.emit("process-requested", msg="Processing was started from the Upload page"
                         + (" (a lock left by an earlier run was cleared)" if cleared else ""))
    start_runner(restart=True)
    if running:
        return "Processing was stopped and started again. Whatever it had not finished is picked up again."
    if cleared:
        return "Nothing was running (an earlier run had left its lock behind, now cleared). Processing has started."
    return "Nothing was running. Processing has started."


# Time left and cost, from the batches measured so far (scripts/wiki_estimate.py). The
# tracker reads only what the event log gained since the last poll.
ESTIMATE = wiki_estimate.Tracker()


def estimate(cfg, queue, runner):
    try:
        ESTIMATE.read([os.path.join(STATE_DIR, "events.1.jsonl"), wiki_events.EVENTS])
        fast = wiki_settings.reading(cfg) == "fast"
        eff = wiki_settings.effective(cfg)
        return ESTIMATE.estimate(len(queue["intake"]), cfg.get("engine"),
                                 1 if fast else eff["parallelBatches"],   # fast: one batch at a time
                                 eff["docsPerBatch"], runner.get("running", False), fast=fast)
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
        "direct": bool(UPLOAD_HELPER),
        # The engine's times carry no zone; this is the zone they are in ("+0800"), so a
        # browser elsewhere (a cloud wiki on a server in UTC) times its steps right.
        "tz": time.strftime("%z"),
        # The credits service's name as its owners know it (wiki_claude's, WIKI_CREDITS_NAME).
        "creditsName": (os.environ.get("WIKI_CREDITS_NAME") or "").strip()[:40] or "Nucleus",
        "queue": queue,
        "needsReview": list_queue("raw/_needs-review"),
        "review": {"open": wiki_review.count_open(), "rev": wiki_review.revision()},
        "actions": {"open": wiki_actions.count_open(), "rev": wiki_actions.revision()},
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
    live = LIVE_SCRIPT.replace("@SITE_BASE@", SITE_BASE).encode()
    return html[:i] + live + html[i:] if i >= 0 else html + live


# ------------------------------------------------------------------- settings ------
CONFIG_LOCK = threading.Lock()
# What the Settings panel may change: the performance mode ("performance"), and figures by
# hand, key -> (type, low, high), the ranges from scripts/wiki_settings.py. A figure set by
# hand wins over the mode's (Settings then shows "Custom"); choosing a mode drops them.
# Everything else in wiki.config.json (engine, port, the local model) stays as the
# installer wrote it.
SETTABLE = {k: (kind,) + wiki_settings.LIMITS[k] for k, kind in
            (("parallelBatches", int), ("docsPerBatch", int), ("maxUploadMb", int), ("maxSpendPerBatchUsd", float))}
LABELS = {"parallelBatches": "batches read at once", "docsPerBatch": "documents per batch",
          "maxUploadMb": "the upload limit in MB", "maxSpendPerBatchUsd": "the spending cap per batch in USD"}
READINGS = ("fast", "classic", "local")


def number(v, kind, default):
    if isinstance(v, bool):
        return default
    try:
        return kind(v)
    except (TypeError, ValueError, OverflowError):
        return default


def shown_settings(cfg):
    """What Settings shows of a config: the figures in effect, whatever set them."""
    eff = wiki_settings.effective(cfg)
    return {
        "title": cfg.get("title") or "",
        "company": cfg.get("company") or "",
        "pipeline": "classic" if cfg.get("pipeline") == "classic" else "fast",
        "performance": wiki_settings.shown_mode(cfg),
        # Fast reading has batches of its own size (fast.docsPerBatch); this is the one in use.
        "parallelBatches": eff["parallelBatches"],
        "docsPerBatch": eff["docsPerBatch"],
        "maxUploadMb": eff["maxUploadMb"],
        "maxSpendPerBatchUsd": eff["maxSpendPerBatchUsd"],
        "review": wiki_jev.review_settings(cfg),
    }


def settings_view():
    cfg = load_config()
    view = shown_settings(cfg)
    view.update({
        "engine": cfg.get("engine") or "claude",
        "reading": wiki_settings.reading(cfg),
        "modes": wiki_settings.presets_for(cfg),   # each mode's figures for this wiki's reading
        "modesByReading": {rd: {m: wiki_settings.preset(m, rd) for m in wiki_settings.MODES} for rd in READINGS},
        "speedMax": wiki_settings.SPEED_MAX,
        "jev": {"available": wiki_jev.available(cfg), "key": bool(KEY.get())},
    })
    return view


def clean_text(v, limit):
    v = "".join(ch for ch in str(v) if ch >= " " and ch != "\x7f").strip()
    return v[:limit]


def change_settings(body):
    """Apply the owner's changes to wiki.config.json. Returns (changed, errors): changed
    lists what Settings now shows differently ("performance" when the mode shown changed,
    a figure when the one in effect changed). A body with a mode and figures applies the
    mode first, then the figures."""
    if not isinstance(body, dict):
        return [], ["the settings were not sent as an object"]
    errors, updates, perf = [], {}, None
    if "performance" in body:
        if isinstance(body["performance"], str) and body["performance"] in wiki_settings.MODES:
            perf = body["performance"]
        else:
            errors.append("the performance mode must be " + ", ".join(wiki_settings.MODES[:-1])
                          + " or " + wiki_settings.MODES[-1])
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
        elif {"mode": mode, "autoConfidence": round(bar, 2)} != r:
            # Only an actual change is saved: a mode written with every other change would
            # look like a choice the owner made (the installer turns Auto-review on unless
            # the owner chose).
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
        if not isinstance(cfg, dict):
            return [], ["wiki.config.json could not be read; fix or restore it first"]
        before = copy.deepcopy(cfg)
        if perf:
            wiki_settings.apply_mode(cfg, perf)   # its figures, for every way of reading
        pipeline = updates.get("pipeline", cfg.get("pipeline")) or "fast"
        if "docsPerBatch" in updates and cfg.get("engine") != "local" and pipeline == "fast":
            # Fast reading's batches are its own (fast.docsPerBatch), not the classic size.
            fast_cfg = dict(cfg.get("fast") or {}) if isinstance(cfg.get("fast"), dict) else {}
            fast_cfg["docsPerBatch"] = updates.pop("docsPerBatch")
            updates["fast"] = fast_cfg
        cfg.update(updates)
        if "maxSpendPerBatchUsd" in updates:
            cfg.pop("maxSpendPerRunUsd", None)   # the older name, now replaced
        old, new = shown_settings(before), shown_settings(cfg)
        changed = [k for k in new if old[k] != new[k]]
        if cfg != before:
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
        items = wiki_review.list_open(places=False)
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
        "done": wiki_review.list_done(100),
        "doneTotal": wiki_review.count_done(),
        "engine": cfg.get("engine") or "claude",
        "review": wiki_jev.review_settings(cfg),
        "jev": {"available": wiki_jev.available(cfg), "key": bool(KEY.get()),
                "busy": WORKER.busy, "error": WORKER.error},
        "rev": wiki_review.revision(),
    }


def actions_view():
    cfg = load_config()
    return {
        "items": wiki_actions.list_open(),
        "done": wiki_actions.list_done(50),
        # Actions come from Claude applying review answers; the model on this Mac makes none.
        "available": (cfg.get("engine") or "claude") != "local",
        "rev": wiki_actions.revision(),
    }


ASK_LOCK = threading.Lock()   # one question at a time
ASK_SECONDS = 2 * wiki_ask.TIMEOUT + 60


def ask_view():
    cfg = load_config()
    return {"available": wiki_ask.available(cfg), "busy": ASK_LOCK.locked(),
            "history": wiki_ask.history()}


def ask(question):
    """One question, answered by scripts/wiki_ask.py in its own process (it reads the
    Keychain's sign-in and calls Claude; this server never does either). (status, body)."""
    question = " ".join(str(question or "").split())
    if not question:
        return 400, {"ok": False, "message": "Type a question first."}
    if len(question) > wiki_ask.QUESTION_CHARS:
        return 400, {"ok": False, "message": f"Keep the question under {wiki_ask.QUESTION_CHARS} characters."}
    if not wiki_ask.available(load_config()):
        return 409, {"ok": False, "message": "This wiki reads with the model on this Mac, so nothing is sent to "
                                             "Claude. Asking needs Claude."}
    if not ASK_LOCK.acquire(blocking=False):
        return 409, {"ok": False, "message": "Still answering the last question. Ask again when it is done."}
    try:
        try:
            r = subprocess.run([sys.executable, os.path.join(WIKI_DIR, "scripts", "wiki_ask.py"), "ask"],
                               input=json.dumps({"question": question}), capture_output=True, text=True,
                               cwd=WIKI_DIR, timeout=ASK_SECONDS)
            res = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None
        except subprocess.TimeoutExpired:
            res = {"ok": False, "note": "No answer came in time. Try again, or ask a shorter question."}
        except (OSError, ValueError, IndexError):
            res = None
        if not isinstance(res, dict):
            return 500, {"ok": False, "message": "The question could not be answered (the asking step failed)."}
        if not res.get("ok"):
            return 200, {"ok": False, "message": res.get("note") or "The question could not be answered."}
        rec = wiki_ask.remember(res)
        wiki_events.emit("ask", msg=f"Answered a question from {res.get('pages', 0)} pages",
                         cost=res.get("cost"), seconds=res.get("seconds"))
        return 200, {"ok": True, "answer": rec}
    finally:
        ASK_LOCK.release()


def rebuild_site():
    """Rebuild the site in the background (the title is built into its pages). A run in
    progress rebuilds at its end anyway; this one then exits at once."""
    subprocess.Popen(["/bin/bash", os.path.join(WIKI_DIR, "scripts", "wiki_runner.sh"), "--rebuild"],
                     cwd=WIKI_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def start_runner(restart=False):
    """Start the runner now. Under launchd the viewer's label names the runner's. restart:
    the runner was just stopped, so launchd is told to start it even if it has not yet
    noticed (kickstart -k)."""
    label = os.environ.get("XPC_SERVICE_NAME", "")
    if label.endswith(".viewer"):
        runner = label[: -len(".viewer")] + ".runner"
        try:
            r = subprocess.run(["launchctl", "kickstart"] + (["-k"] if restart else [])
                               + [f"gui/{os.getuid()}/{runner}"],
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
RUN_START = b'"type": "run-start"'


def last_run_start(path, run):
    """Byte offset of the run-start event of `run` in the log, or None (not there, or the
    last run-start belongs to another run)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    i = data.rfind(RUN_START)
    if i < 0:
        return None
    start = data.rfind(b"\n", 0, i) + 1
    end = data.find(b"\n", i)
    try:
        rec = json.loads(data[start:end if end >= 0 else len(data)])
    except ValueError:
        return None
    return start if isinstance(rec, dict) and rec.get("run") == run else None


def history(path, lines):
    """Where the page's history begins: the last `lines` lines, or, while a run is going,
    from that run's start if it began earlier (a large upload writes thousands of events,
    and a page opened in the middle needs every file's). Returns (offset in the log, lines
    from the rotated log that come first: the run started before the log rotated)."""
    off = history_offset(path, lines)
    # The run going now (its id is on the live line from its first step); a rebuild, or a
    # run still settling before its run-start, replays nothing older than the usual lines.
    run = (wiki_events.read_now() or {}).get("run") if runner_state()["running"] else None
    if not run:
        return off, []
    start = last_run_start(path, run)
    if start is not None:
        return min(off, start), []
    old = path[: -len(".jsonl")] + ".1.jsonl"
    begin = last_run_start(old, run)
    if begin is None:
        return off, []
    with open(old, "rb") as f:
        f.seek(begin)
        earlier = [ln for ln in f.read().split(b"\n") if ln.strip()]
    return 0, earlier


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
                           "site": SITE_BASE or self._origin_for(self.server.site_port), "app": APP_BASE})
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
                if rel == "api/actions":
                    return self._send_json(200, actions_view())
                if rel == "api/settings":
                    return self._send_json(200, settings_view())
                if rel == "api/ask":
                    return self._send_json(200, ask_view())
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
        earlier = []
        try:
            offset = int(since)
        except ValueError:
            offset, earlier = history(path, HISTORY_LINES)
        self._headers(200, "text/event-stream; charset=utf-8", None, {"X-Frame-Options": "DENY"})
        self.wfile.write(b"retry: 2000\n\n")
        if earlier:   # no id: a reconnect resumes in the current log
            self.wfile.write(b"".join(b"data: " + ln + b"\n\n" for ln in earlier))
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
        if rel == "api/upload/direct" and UPLOAD_HELPER:
            self._drain_small()
            return self._upload_direct(query)
        if rel == "api/upload/direct/done" and UPLOAD_HELPER:
            self._drain_small()
            return self._upload_direct_done(query)
        if rel == "api/process":
            wiki_events.emit("process-requested")
            return self._send_json(200, {"ok": True, "runner": start_runner()})
        if rel == "api/restart":
            self._drain_small()
            return self._send_json(200, {"ok": True, "message": restart()})
        if rel in ("api/review/answer", "api/review/reopen", "api/review/score", "api/actions/update",
                   "api/settings", "api/settings/jev-key", "api/ask", "api/ask/save", "api/ask/correct"):
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

    def _ask(self, body):
        st, res = ask(body.get("question"))
        return self._send_json(st, res)

    def _ask_save(self, body):
        res = wiki_ask.save(str(body.get("id") or ""))
        if res["ok"] and res.get("packet") and not res["message"].startswith("Already"):
            start_runner()
        return self._send_json(200 if res["ok"] else 409, res)

    def _ask_correct(self, body):
        res = wiki_ask.correct(str(body.get("id") or ""), str(body.get("text") or "")[:2000])
        if res["ok"]:
            start_runner()
        return self._send_json(200 if res["ok"] else 409, res)

    def _review_reopen(self, body):
        res = wiki_review.reopen(str(body.get("id") or ""))
        return self._send_json(200 if res["ok"] else 409, {"ok": res["ok"], "message": res["message"]})

    def _actions_update(self, body):
        iid = body.get("id")
        fields = {}
        if "assignee" in body:
            a = body["assignee"] if body["assignee"] is not None else ""
            if not isinstance(a, str) or a not in wiki_actions.ASSIGNEES:
                return self._send_json(400, {"ok": False, "message": "The assignee must be ai_agent, employee, "
                                                                     "external, or empty for no one."})
            fields["assignee"] = a
        if "status" in body:
            if not isinstance(body["status"], str) or body["status"] not in wiki_actions.STATUSES:
                return self._send_json(400, {"ok": False, "message": "The status must be open, done or dismissed."})
            fields["status"] = body["status"]
        if not isinstance(iid, str) or not iid:
            return self._send_json(400, {"ok": False, "message": "Say which action (its id)."})
        if not fields:
            return self._send_json(400, {"ok": False, "message": "Nothing to change: send an assignee or a status."})
        res = wiki_actions.update(iid, **fields)
        return self._send_json(409 if res.get("missing") else 200, {"ok": res["ok"], "message": res["message"]})

    def _review_score(self, body):
        if not wiki_jev.available():
            return self._send_json(409, {"ok": False, "message":
                                         "This wiki reads with the model on this Mac, so nothing is sent to Jev."})
        if not KEY.get():
            return self._send_json(409, {"ok": False, "message": "Add a TypeSafe API key in Settings first."})
        ids = body.get("ids")
        ids = [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else \
            [i["id"] for i in wiki_review.list_open(places=False)]
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

    def _upload_direct(self, query):
        name, why = clean_upload_name((query.get("name") or [""])[0])
        if name is None:
            return self._send_json(200, {"ok": False, "skipped": why})
        try:
            size = int((query.get("size") or [""])[0])
        except ValueError:
            return self._send_json(400, {"ok": False, "error": "the upload had no size"})
        limit = max_upload_bytes()
        if size > limit:
            return self._send_json(413, {"ok": False, "error":
                                         f"larger than the {round(limit / 1024 / 1024)} MB limit"})
        try:
            got = upload_helper("/presign", {"size": size})
        except (OSError, ValueError):
            return self._send_json(502, {"ok": False, "error": "the upload could not be started"})
        return self._send_json(200, {"ok": True, "url": got["url"], "headers": got.get("headers") or {},
                                     "key": got["key"]})

    def _upload_direct_done(self, query):
        name, why = clean_upload_name((query.get("name") or [""])[0])
        if name is None:
            return self._send_json(200, {"ok": False, "skipped": why})
        key = (query.get("key") or [""])[0]
        os.makedirs(UPLOAD_TMP, exist_ok=True)
        tmp = os.path.join(UPLOAD_TMP, f".upload-{secrets.token_hex(8)}.part")
        try:
            got = upload_helper("/fetch", {"key": key, "dest": tmp, "limit": max_upload_bytes()})
            if not got.get("ok"):
                raise OSError(got.get("error") or "not fetched")
            queue, dest, duplicate = place_upload(tmp, name)
        except (OSError, ValueError):
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return self._send_json(500, {"ok": False, "error": "could not save the file"})
        path = os.path.relpath(dest, WIKI_DIR).replace(os.sep, "/")
        wiki_events.emit("upload", file=path, name=name, queue=queue, bytes=got.get("bytes"),
                         duplicate=True if duplicate else None)
        return self._send_json(200, {"ok": True, "file": path, "queue": queue,
                                     "duplicate": duplicate})

    def _drain_small(self):
        """Read and ignore a small body (a POST that needs none may still send {})."""
        try:
            left = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            left = 0
        if 0 < left <= 65536:
            self.rfile.read(left)
        elif left > 65536:
            self.close_connection = True

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
