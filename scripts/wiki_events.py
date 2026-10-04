#!/usr/bin/env python3
"""wiki_events.py — the activity log behind the Upload page.

Every step of a run is one JSON line in .wiki-engine/state/events.jsonl. The viewer
(wiki_server.py) streams that file to the Upload page, which turns it into per-file
progress and a live log. Standard library only.

  wiki_events.py emit TYPE [key=value ...] [key:=json ...]
      Append one event. key=value stores text; key:=value stores parsed JSON (a number,
      a list).

  wiki_events.py emit-each TYPE --key K [--key K ...] [--progress] [key=value ...]
      Append one event per line of stdin, all under one lock (thousands of files cost
      a fraction of a second, not a process each). A line's tab-separated values go to
      the --key fields in order (with one --key, the whole line: a file name may hold a
      tab); key=value and key:=json fields go on every event.
      --progress: the live line's current step counts them (n of the total).

  wiki_events.py claude-stream --file F --pid P [--label L] [--batch B]
      Follow Claude's `--output-format stream-json` output in F until process P has
      exited, and turn each completed action into an event:
        Read of a document's Markdown mirror  -> read     (the document being read)
        Write / Edit under wiki/              -> page     (created or updated)
        Write of review/open/*.json           -> review-item
        Write of actions/open/*.json          -> action-item (a suggested action)
        mv out of raw/_intake/                -> filed    (and the document's review items
                                                           follow it: wiki_review.relocate)
        mv out of raw/inbox/                  -> applied
        the final result                      -> claude-end (turns, cost)
      Actions are reported only once their tool call succeeded. Plain-language lines also
      go to the runner log.

  wiki_events.py convert-stream [--total N] [--start K] [--quiet] [--list FILE]
      Read scripts/to_markdown.py output on stdin, copy it to the runner log, and emit a
      convert event for each document it starts converting and a converted event when it
      is done. --start: documents converted in earlier batches, so numbering runs on.
      With --list, run to_markdown.py on the sources listed in FILE itself, and watch it:
        - output that ends without to_markdown's closing summary means the converter
          died on the file it was on (a native crash; on a Mac, "Python quit
          unexpectedly"): that file gets an ERROR mirror, so it is read as unreadable
          and never converted again in a loop, and to_markdown starts again for the
          rest of the list (it skips what is done), at most MAX_CONVERT_RESTARTS times;
          a file whose ERROR mirror cannot be written either (a name too long for
          "<name>.md") is left out of the list it starts again on, so it can never use
          up the restarts: it stays without text, and is read (and set aside) as such;
        - no output for --stall seconds (WIKI_CONVERT_STALL_SECONDS, 30 minutes; a
          tool at work prints a line every minute) means it hangs: it is stopped with
          everything it started (the tool it runs too), the file gets an ERROR mirror
          (timeout), and the rest goes on the same way. The silence is measured on a
          monotonic clock and starts over after the Mac slept, so closing the lid never
          makes a conversion look hung;
        - a converter that dies or hangs before it reaches any file cannot work at all
          (a library that fails as it loads): exit status 3, and the runner says so
          instead of starting it again and again.

  wiki_events.py now PHASE [key=value ...] [key:=json ...]
      Say what is happening right now (see set_now).

The log rotates at about 5 MB (one previous file is kept).

Next to the log, .wiki-engine/state/now.json holds one record: what the engine is doing
at this moment (converting a file, OCR, a question to the model, writing a page, ...).
It is replaced, never appended to, so it can change every second without growing the
log; the Upload page shows it as its live line.

.wiki-engine/state/progress is touched only when something actually moves: an event of a
run (WIKI_RUN_ID set), or a live-line step whose phase, file, detail, count or tokens
change. A record repeated unchanged (a "still reading" every 30 seconds) does not touch it,
so its age is how long the work has shown no progress: the runner's watchdog and the
viewer's "Restart processing" offer both read it. Conversion running ahead of the reading
(WIKI_CONVERT_AHEAD set, by the runner, for every batch after the first) touches
.wiki-engine/state/convert-progress instead: its events must not hide a reader that hangs.
"""
import argparse
import datetime
import fcntl
import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

import wiki_netguard

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(WIKI_DIR, ".wiki-engine", "state")
EVENTS = os.path.join(STATE_DIR, "events.jsonl")
NOW = os.path.join(STATE_DIR, "now.json")
PROGRESS = os.path.join(STATE_DIR, "progress")
CONVERT_PROGRESS = os.path.join(STATE_DIR, "convert-progress")   # conversion ahead of the reading
PROGRESS_KEYS = ("phase", "file", "detail", "n", "of", "tokens")
LOG_FILE = os.path.join(
    os.environ.get("WIKI_LOG_DIR") or os.path.expanduser("~/Library/Logs/wiki-starter"), "runner.log"
)
ROTATE_BYTES = 5 * 1024 * 1024
CONVERTER = os.path.join(WIKI_DIR, "scripts", "to_markdown.py")
CONVERT_STALL_SECONDS = 1800   # a conversion silent this long is stopped (WIKI_CONVERT_STALL_SECONDS)
MAX_CONVERT_RESTARTS = 20      # times one list's conversion starts again after a crash or a hang
CONVERTER_BROKEN = 3           # convert-stream --list: the converter cannot work at all (exit status)


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _record(etype, fields):
    rec = {"t": now(), "type": etype}
    rec.update({k: v for k, v in fields.items() if v not in (None, "")})
    if not rec.get("run"):
        run = os.environ.get("WIKI_RUN_ID")
        if run:
            rec["run"] = run
    return rec


def progressed():
    """Something moved: touch the progress marker (convert-progress for conversion running
    ahead of the reading, which is not the reading's progress)."""
    path = CONVERT_PROGRESS if os.environ.get("WIKI_CONVERT_AHEAD") else PROGRESS
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(path, "a"):
            pass
        os.utime(path, None)
    except OSError:
        pass


def _append(chunks, between=None):
    """Add text to the log under one exclusive lock (the runner and the viewer both write);
    between(n) is called after each chunk. An event of a run is progress."""
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.environ.get("WIKI_RUN_ID"):
        progressed()
    with open(EVENTS + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if os.path.exists(EVENTS) and os.path.getsize(EVENTS) > ROTATE_BYTES:
                os.replace(EVENTS, EVENTS[: -len(".jsonl")] + ".1.jsonl")
            with open(EVENTS, "a", encoding="utf-8") as f:
                for i, text in enumerate(chunks, 1):
                    f.write(text)
                    if between:
                        f.flush()
                        between(i)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def emit(etype, **fields):
    """Append one event."""
    rec = _record(etype, fields)
    _append([json.dumps(rec, ensure_ascii=False) + "\n"])
    return rec


def emit_each(etype, keys, rows, common=None, progress=False):
    """One event per row (a list of values for `keys`), each also carrying `common`, all
    appended under one lock. Returns how many were written."""
    lines = []
    for values in rows:
        fields = dict(common or {})
        fields.update(zip(keys, values))
        if any(fields.get(k) for k in keys):
            lines.append(json.dumps(_record(etype, fields), ensure_ascii=False) + "\n")
    step = 500
    chunks = ["".join(lines[i:i + step]) for i in range(0, len(lines), step)]
    tell = (lambda i: update_now(n=min(i * step, len(lines)), of=len(lines))) if progress else None
    if chunks:
        _append(chunks, tell)
    return len(lines)


def read_now():
    try:
        with open(NOW, encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_now(rec):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = f"{NOW}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, NOW)  # readers see the old record or the new one, never half of one
    trace = os.environ.get("WIKI_NOW_TRACE")  # for tests and debugging: keep every record
    if trace:
        with open(trace, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def set_now(phase, **fields):
    """Replace the live record: the phase (convert, ocr, load, read, ask, check, write,
    file, apply, search, think, settle, tidy, commit, build, idle) and plain facts about
    it (file, detail, n and of, pages, tokens). `since` restarts when the step changes
    (phase, file or detail), so the elapsed time shown is the time spent on this step;
    a step whose detail tells facts that change (settling) gives its own `since`."""
    prev = read_now()
    t = now()
    rec = {"t": t, "phase": phase}
    rec.update({k: v for k, v in fields.items() if v not in (None, "")})
    run = os.environ.get("WIKI_RUN_ID")
    if run and phase != "idle":
        rec["run"] = run
    batch = os.environ.get("WIKI_BATCH")  # "2/22" while the runner reads in batches
    if batch and phase != "idle":
        rec.setdefault("batch", batch)
    if not rec.get("since"):
        same = all(prev.get(k) == rec.get(k) for k in ("phase", "file", "detail"))
        rec["since"] = prev.get("since") if same and prev.get("since") else t
    if any(prev.get(k) != rec.get(k) for k in PROGRESS_KEYS):
        progressed()
    try:
        _write_now(rec)
    except OSError:
        pass
    return rec


def update_now(**fields):
    """Add facts to the current step (tokens so far, pages) without restarting it."""
    rec = read_now()
    if not rec:
        return {}
    new = {k: v for k, v in fields.items() if v not in (None, "")}
    if any(k in PROGRESS_KEYS and rec.get(k) != v for k, v in new.items()):
        progressed()
    rec.update(new)
    rec["t"] = now()
    try:
        _write_now(rec)
    except OSError:
        pass
    return rec


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def rel(path):
    """A path as the wiki folder sees it: relative, forward slashes, no ./ prefix."""
    if not isinstance(path, str) or not path:
        return ""
    p = path.strip()
    root = os.path.realpath(WIKI_DIR)
    for base in (WIKI_DIR, root):
        if p == base:
            return ""
        if p.startswith(base + os.sep):
            p = p[len(base) + 1 :]
            break
    while p.startswith("./"):
        p = p[2:]
    return p


def mirror_to_source(path):
    """cache/md/_intake/a/b.pdf.md -> raw/_intake/a/b.pdf (the converter's naming), and
    cache/md/_intake/a/IMG_1.HEIC.jpg -> raw/_intake/a/IMG_1.HEIC (a photo's JPEG copy)."""
    if path.startswith("cache/md/") and path.endswith(".md"):
        return "raw/" + path[len("cache/md/") : -len(".md")]
    if path.startswith("cache/md/") and path.lower().endswith((".heic.jpg", ".heif.jpg")):
        return "raw/" + path[len("cache/md/") : -len(".jpg")]
    return ""


def review_item(content, text_key="question"):
    """(id, question) from a review item Claude wrote, or None. text_key="title" reads a
    suggested action the same way."""
    try:
        item = json.loads(content or "")
    except ValueError:
        return None
    if not isinstance(item, dict):
        return None
    return str(item.get("id") or ""), " ".join(str(item.get(text_key) or "").split())[:200]


# ------------------------------------------------------------- claude stream -------
class ClaudeStream:
    """Turns Claude Code stream-json messages into events. Tolerates unknown shapes."""

    def __init__(self, label, batch=None):
        self.label = label
        self.pending = {}  # tool_use id -> (name, input)
        self.current = ""  # the document or packet being worked on
        self.done = False
        # The documents this session was given. Another document it reads (one in
        # another batch, for context) is looking something up, not working on it.
        self.batch = batch

    def mine(self, src):
        return self.batch is None or src in self.batch

    def handle(self, msg):
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        # "message" is a dict on assistant and user lines, but plain text on some others
        # (an error or a notice): that line carries no content.
        message = msg.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "assistant" and isinstance(content, list):
            started = False
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.pending[block.get("id")] = (block.get("name"), block.get("input") or {})
                    self.on_start(block.get("name"), block.get("input") or {})
                    started = True
            if not started and any(isinstance(b, dict) and b.get("type") in ("text", "thinking")
                                   for b in content):
                set_now("think", file=self.current, detail="Claude is thinking")
        elif kind == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    call = self.pending.pop(block.get("tool_use_id"), None)
                    if call and not block.get("is_error"):
                        self.on_tool(*call)
        elif kind == "result":
            self.done = True
            turns, cost = msg.get("num_turns"), msg.get("total_cost_usd")
            emit("claude-end", label=self.label, turns=turns, cost=cost,
                 ok=not msg.get("is_error") and msg.get("subtype") == "success",
                 detail=msg.get("subtype"))
            text = msg.get("result")
            if isinstance(text, str) and text.strip():
                log(f"claude {self.label}: {text.strip()[:2000]}")
            log(f"claude {self.label}: finished ({msg.get('subtype')}, {turns} turns, ${cost})")

    def on_start(self, name, inp):
        """The live line: what Claude has just started doing."""
        if name == "Read":
            path = rel(inp.get("file_path", ""))
            src = mirror_to_source(path)
            if src.startswith("raw/_intake/") and self.mine(src):
                photo = path.lower().endswith(".jpg")
                set_now("read", file=src, detail="Claude is looking at the photo" if photo
                        else "Claude is reading it")
            elif path.startswith("raw/_intake/") and self.mine(path):
                set_now("read", file=path, detail="Claude is looking at the original")
            elif path.startswith("raw/inbox/"):
                set_now("read", file=path, detail="Claude is reading the update")
            else:
                set_now("search", file=self.current, detail=f"reading {path}")
        elif name in ("Glob", "Grep"):
            what = inp.get("pattern") or inp.get("path") or ""
            set_now("search", file=self.current, detail=f"searching the wiki for {what}".strip())
        elif name in ("Write", "Edit", "MultiEdit"):
            path = rel(inp.get("file_path", ""))
            target = ("the review queue" if path == "wiki/_review.md" or path.startswith("review/")
                      else "a suggested action" if path.startswith("actions/")
                      else path.replace("wiki/", "", 1))
            set_now("write", file=self.current, detail=f"writing {target}")
        elif name == "Bash":
            try:
                words = shlex.split(inp.get("command", "") or "")
            except ValueError:
                words = []
            if words[:1] == ["mv"]:
                args = [w for w in words[1:] if not w.startswith("-")]
                to = rel(args[-1]) if len(args) == 2 else ""
                set_now("file", file=rel(args[0]) if args else self.current,
                        detail=f"moving it to {to.rstrip('/')}" if to else "")
            elif words[:1] == ["mkdir"]:
                set_now("file", file=self.current, detail="making a folder")
            else:
                set_now("search", file=self.current, detail="looking around the folder")

    def on_tool(self, name, inp):
        if name == "Read":
            path = rel(inp.get("file_path", ""))
            src = mirror_to_source(path)
            if src.startswith("raw/_intake/") and self.mine(src):
                self.set_current(src)
            elif path.startswith("raw/_intake/") and self.mine(path):
                self.set_current(path)
            elif path.startswith("raw/inbox/") and path.endswith((".md", ".markdown")):
                self.set_current(path)
        elif name in ("Write", "Edit", "MultiEdit"):
            path = rel(inp.get("file_path", ""))
            if path.startswith("review/open/") and path.endswith(".json") and name == "Write":
                found = review_item(inp.get("content"))
                if found:
                    emit("review-item", file=self.current, id=found[0], question=found[1])
                    log(f"  question for review: {found[1]} ({found[0]})")
            elif path.startswith("actions/open/") and path.endswith(".json") and name == "Write":
                found = review_item(inp.get("content"), "title")
                if found:
                    emit("action-item", file=self.current, id=found[0], title=found[1])
                    log(f"  suggested action: {found[1]} ({found[0]})")
            elif path.startswith("wiki/") or path in ("index.md", "log.md"):
                if path == "wiki/_review.md":
                    emit("review", file=self.current, page=path)
                    log(f"  noted for review ({self.current or 'general'})")
                elif path.startswith("wiki/"):
                    action = "created" if name == "Write" else "updated"
                    emit("page", file=self.current, page=path, action=action)
                    log(f"  {action} {path}")
        elif name == "Bash":
            self.on_shell(inp.get("command", ""))

    def on_shell(self, command):
        try:
            words = shlex.split(command or "")
        except ValueError:
            return
        if len(words) < 3 or words[0] != "mv":
            return
        args = [w for w in words[1:] if not w.startswith("-")]
        if len(args) != 2:
            return
        src, dst = rel(args[0]), rel(args[1])
        if dst.endswith("/") or os.path.isdir(os.path.join(WIKI_DIR, dst)):
            dst = dst.rstrip("/") + "/" + os.path.basename(src)
        if src.startswith("raw/_intake/"):
            emit("filed", file=src, to=dst)
            log(f"  filed {src} -> {dst}")
            try:   # its review items now name where it is (imported here: it imports this module)
                import wiki_review
                wiki_review.relocate(src, dst)
            except Exception as e:  # never let the bookkeeping stop the stream
                log(f"  review items for {src} not updated: {e!r}")
        elif src.startswith("raw/inbox/"):
            emit("applied", file=src, to=dst)
            log(f"  applied {src} -> {dst}")
        if src == self.current:
            self.current = ""

    def set_current(self, path):
        if path != self.current:
            self.current = path
            emit("read", file=path)
            log(f"  reading {path}")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def follow(path, pid, label, poll=0.5, current_path="", batch_path=""):
    """Turn one Claude session's stream into events and live-line records. With
    current_path, also keep the file this session is working on there: several sessions
    can run at once, so the shared live line cannot say which file a stalled one was on.
    With batch_path, only the documents listed there count as this session's."""
    batch = None
    if batch_path:
        try:
            with open(batch_path, encoding="utf-8") as f:
                batch = {ln.rstrip("\n") for ln in f if ln.rstrip("\n")}   # a name may end in a space
        except OSError:
            batch = None
    stream = ClaudeStream(label, batch)
    buf, offset, last = "", 0, None
    while True:
        alive = pid_alive(pid)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
        except FileNotFoundError:
            chunk = ""
        if chunk:
            buf += chunk
            lines = buf.split("\n")
            buf = lines.pop()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    stream.handle(json.loads(line))
                except (ValueError, TypeError, AttributeError):
                    log(f"claude {label}: {line[:500]}")
        elif not alive:
            if buf.strip():
                try:
                    stream.handle(json.loads(buf))
                except (ValueError, TypeError, AttributeError):
                    log(f"claude {label}: {buf.strip()[:500]}")
            break
        if current_path and stream.current != last:
            last = stream.current
            try:
                with open(current_path, "w", encoding="utf-8") as f:
                    f.write(last)
            except OSError:
                pass
        if not chunk:
            time.sleep(poll)
    if not stream.done:
        emit("claude-end", label=label, ok=False, detail="stopped")


# ----------------------------------------------------------- convert stream --------
def ignored_name(name):
    """The runner's rule (is_ignored_name in wiki_runner.sh): files it never processes."""
    return (name.startswith((".", "Icon", "~$")) or name == "README.md"
            or name.endswith((".download", ".crdownload", ".part", ".partial", ".tmp")))


class ConvertProgress:
    """to_markdown.py's output, line by line, as events and live-line records. quiet: leave
    the live line alone (conversion running ahead in the background, while the live line
    belongs to the reading)."""

    def __init__(self, total=0, start=0, quiet=False):
        self.say = (lambda *a, **k: None) if quiet else set_now
        self.total, self.seen = total, start
        self.current = ""      # the document being converted
        self.summary = False   # to_markdown's closing line: it finished its list
        self.unwritten = set() # files whose mirror could not be written (not even an ERROR one)

    def line(self, line):
        log(line.rstrip("\n"))
        s = line.strip()
        if s.startswith("[") and "] " in s:
            head, _, name = line.rstrip("\n").lstrip().partition("] ")   # a name may end in a space
            if head[1:].isdigit() and name and not ignored_name(os.path.basename(name)):
                self.done()
                self.current = "raw/" + name
                self.seen += 1
                emit("convert", file=self.current)
                self.say("convert", file=self.current, n=self.seen, of=self.total or None,
                         detail="converting it to text")
        elif s.startswith("could not write the mirror") and self.current:
            # Not converted, and no ERROR mirror either: it is read without its text.
            src, self.current = self.current, ""
            self.unwritten.add(src)
            why = s.partition(": ")[2][:150]
            emit("error", file=src, msg=f"{os.path.basename(src)}: its text could not be saved ({why})")
        elif s.startswith("ocr: ") and self.current:
            pages = s[len("ocr: "):].split(" ", 1)[0]
            pages = int(pages) if pages.isdigit() else None
            emit("ocr", file=self.current, pages=pages)
            self.say("ocr", file=self.current, pages=pages, n=self.seen, of=self.total or None,
                     detail="reading the scanned pages with OCR")
        elif "converted=" in s and "skipped(up-to-date)=" in s:
            self.summary = True

    def done(self):
        if self.current:
            emit("converted", file=self.current)
        self.current = ""

    def died(self, why):
        """The converter stopped without finishing (it crashed, or was stopped for hanging):
        the file it was on gets an ERROR mirror, unless its mirror was written after all.
        Returns that file ("" if there was none). One whose ERROR mirror cannot be written
        either goes into self.unwritten: to_markdown must not start again on it."""
        src, self.current = self.current, ""
        if not src:
            return ""
        rel = src[len("raw/"):]
        mirror = os.path.join(WIKI_DIR, "cache", "md", rel + ".md")
        try:
            finished = os.path.getmtime(mirror) >= os.path.getmtime(os.path.join(WIKI_DIR, src))
        except OSError:
            finished = False
        if finished:
            emit("converted", file=src)
            return src
        try:
            sys.path.insert(0, os.path.dirname(CONVERTER))
            import to_markdown
            to_markdown.write_error(mirror, rel, why)
        except Exception as e:  # never let this stop the rest of the list
            log(f"could not mark {src} as unreadable: {e}")
            self.unwritten.add(src)
        # Not "converted": nothing was. The error says what happened instead.
        then = ("not even a note of that could be saved, so it is read without any text" if src in self.unwritten
                else "it will be read as unreadable")
        emit("error", file=src, msg=f"{os.path.basename(src)}: {why}; {then}")
        log(f"{src}: {why}")
        return src


def convert_stream(stream=sys.stdin, total=0, start=0, quiet=False):
    """Conversion progress as events, from to_markdown.py's output on `stream`."""
    prog = ConvertProgress(total, start, quiet)
    for line in stream:
        prog.line(line)
    if prog.summary:
        prog.done()
    else:
        prog.died("the converter stopped on this file")


def _describe_exit(rc):
    if rc is not None and rc < 0:
        try:
            return f"it was ended by {signal.Signals(-rc).name}"
        except ValueError:
            return f"it was ended by signal {-rc}"
    return f"exit {rc}"


def _descendants(pid):
    """Every process started under pid, by parent, whatever its process group (an outside
    tool runs in a group of its own), from one ps listing; [] if ps cannot say."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    kids = {}
    for row in out.splitlines():
        parts = row.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, todo = [], [pid]
    while todo:
        for child in kids.get(todo.pop(), []):
            if child not in found:
                found.append(child)
                todo.append(child)
    return found


def _stop_tree(p):
    """Stop a converter and everything it started. It runs in its own process group, and
    stops the tool it is running (OCR, in a group of its own) when it gets SIGTERM; for a
    converter that has to be killed outright, the processes under it are looked up first
    and killed after it, so no OCR goes on without a parent."""
    tree = _descendants(p.pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except OSError:
            pass
        try:
            p.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            continue
    for pid in tree:   # gone already, as a rule: to_markdown stopped its tool itself
        for kill in (os.killpg, os.kill):
            try:
                kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _list_without(list_path, skip):
    """A copy of a source list without the files in `skip` (paths as the wiki folder sees
    them), in a temporary file of its own. Returns its path."""
    with open(list_path, encoding="utf-8") as f:
        rows = [ln.rstrip("\n") for ln in f if ln.rstrip("\n")]
    keep = [r for r in rows if os.path.relpath(os.path.join(WIKI_DIR, r), WIKI_DIR) not in skip]
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="convert-rest.", suffix=".txt", dir=STATE_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(r + "\n" for r in keep))
    return path


def convert_run(list_path, total=0, start=0, quiet=False, stall=CONVERT_STALL_SECONDS,
                restarts=MAX_CONVERT_RESTARTS):
    """Run to_markdown.py on the sources in list_path and watch it (see the module's
    usage). Returns 0 when it finished its list; CONVERTER_BROKEN when it cannot work at
    all: it died (or hung) before it reached a single file, so starting it again would do
    the same, or it kept dying, MAX_CONVERT_RESTARTS times; else to_markdown's exit status
    or 1."""
    prog = ConvertProgress(total, start, quiet)
    rc, starts, current, copies = 1, 0, list_path, []
    step = min(5.0, max(0.1, stall))
    try:
        while True:
            starts += 1
            cmd = [sys.executable, CONVERTER, "--only", "_intake", "--list", current]
            try:
                p = subprocess.Popen(cmd, cwd=WIKI_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, errors="replace",
                                     start_new_session=True)
            except OSError as e:
                log(f"to_markdown could not start: {e}")
                return CONVERTER_BROKEN
            lines = queue.Queue()

            def pump(out=p.stdout, q=lines):
                for ln in out:
                    q.put(ln)
                q.put(None)

            threading.Thread(target=pump, daemon=True).start()
            # Silence on a monotonic clock (on a Mac it stops while the Mac sleeps); should a
            # clock go on during sleep, a pass of the loop far longer than its step is the
            # Mac waking up, and the silence starts over.
            last = mark = time.monotonic()
            hung = False
            while True:
                try:
                    ln = lines.get(timeout=step)
                except queue.Empty:
                    ln = False
                now = time.monotonic()
                if now - mark > step + 30:
                    last = now
                mark = now
                if ln is False:
                    if now - last >= stall:
                        hung = True
                        _stop_tree(p)
                        break
                    continue
                if ln is None:
                    break
                last = now
                prog.line(ln)
            rc = p.wait()
            if prog.summary and not hung:
                prog.done()
                return rc
            if hung:
                took = f"{round(stall / 60)} min" if stall >= 60 else f"{round(stall)} s"
                why = f"timeout: the conversion showed no progress for {took} and was stopped"
                log(f"to_markdown showed no progress for {round(stall)}s; stopped it")
            else:
                why = "the converter stopped on this file"
                log(f"to_markdown stopped before the end of its list ({_describe_exit(rc)})")
            src = prog.died(why)
            if not src:
                log("the converter stopped before it reached any file: it cannot work at all")
                return CONVERTER_BROKEN
            if starts > restarts:
                log(f"to_markdown stopped {starts} times on this list; the rest waits for the next run")
                return CONVERTER_BROKEN
            prog.summary = False
            if prog.unwritten:
                # A file without even an ERROR mirror would be converted again first, and
                # stop it again, until the restarts ran out: the rest of the list goes on
                # without it (it is read without its text, counts its tries, is set aside).
                try:
                    current = _list_without(list_path, prog.unwritten)
                    copies.append(current)
                except OSError as e:
                    log(f"could not leave {', '.join(sorted(prog.unwritten))} out of the list: {e}")
                    return CONVERTER_BROKEN
                log(f"starting to_markdown again for the rest of the list, without "
                    f"{', '.join(sorted(prog.unwritten))} (its text cannot be saved)")
            else:
                log("starting to_markdown again for the rest of the list")
    finally:
        for path in copies:
            try:
                os.remove(path)
            except OSError:
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wiki activity log.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("emit")
    e.add_argument("type")
    e.add_argument("fields", nargs="*")
    ee = sub.add_parser("emit-each")
    ee.add_argument("type")
    ee.add_argument("--key", action="append", required=True)
    ee.add_argument("--progress", action="store_true")
    ee.add_argument("fields", nargs="*")
    c = sub.add_parser("claude-stream")
    c.add_argument("--file", required=True)
    c.add_argument("--pid", type=int, required=True)
    c.add_argument("--label", default="")
    c.add_argument("--current", default="")
    c.add_argument("--batch", default="")
    cs = sub.add_parser("convert-stream")
    cs.add_argument("--total", type=int, default=0)
    cs.add_argument("--start", type=int, default=0)
    cs.add_argument("--quiet", action="store_true")
    cs.add_argument("--list", default="")
    cs.add_argument("--stall", type=float,
                    default=float(os.environ.get("WIKI_CONVERT_STALL_SECONDS") or CONVERT_STALL_SECONDS))
    n = sub.add_parser("now")
    n.add_argument("type")
    n.add_argument("fields", nargs="*")
    a, extra = ap.parse_known_args(argv)
    if a.cmd == "emit-each" and all("=" in x for x in extra):
        a.fields += extra   # key=value fields after the --key options
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    wiki_netguard.install()
    fields = {}
    for kv in getattr(a, "fields", None) or []:
        k, sep, v = kv.partition("=")
        if not sep:
            continue
        if k.endswith(":"):
            try:
                fields[k[:-1]] = json.loads(v)
            except ValueError:
                fields[k[:-1]] = v
        else:
            fields[k] = v
    if a.cmd in ("emit", "now"):
        (emit if a.cmd == "emit" else set_now)(a.type, **fields)
    elif a.cmd == "emit-each":
        one = len(a.key) == 1   # one field: the whole line (a file name may hold a tab)
        rows = ([ln.rstrip("\r\n")] if one else ln.rstrip("\r\n").split("\t") for ln in sys.stdin if ln.strip())
        emit_each(a.type, a.key, rows, fields, progress=a.progress)
    elif a.cmd == "claude-stream":
        follow(a.file, a.pid, a.label, current_path=a.current, batch_path=a.batch)
    elif a.list:
        return convert_run(a.list, total=a.total, start=a.start, quiet=a.quiet, stall=a.stall)
    else:
        convert_stream(total=a.total, start=a.start, quiet=a.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
