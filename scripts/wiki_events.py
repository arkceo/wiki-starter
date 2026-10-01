#!/usr/bin/env python3
"""wiki_events.py — the activity log behind the Upload page.

Every step of a run is one JSON line in .wiki-engine/state/events.jsonl. The viewer
(wiki_server.py) streams that file to the Upload page, which turns it into per-file
progress and a live log. Standard library only.

  wiki_events.py emit TYPE [key=value ...] [key:=json ...]
      Append one event. key=value stores text; key:=value stores parsed JSON (a number,
      a list).

  wiki_events.py claude-stream --file F --pid P [--label L]
      Follow Claude's `--output-format stream-json` output in F until process P has
      exited, and turn each completed action into an event:
        Read of a document's Markdown mirror  -> read     (the document being read)
        Write / Edit under wiki/              -> page     (created or updated)
        mv out of raw/_intake/                -> filed
        mv out of raw/inbox/                  -> applied
        the final result                      -> claude-end (turns, cost)
      Actions are reported only once their tool call succeeded. Plain-language lines also
      go to the runner log.

  wiki_events.py convert-stream
      Read scripts/to_markdown.py output on stdin, copy it to the runner log, and emit a
      convert event for each document it starts converting.

The log rotates at about 5 MB (one previous file is kept).
"""
import argparse
import datetime
import fcntl
import json
import os
import shlex
import sys
import time

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(WIKI_DIR, ".wiki-engine", "state")
EVENTS = os.path.join(STATE_DIR, "events.jsonl")
LOG_FILE = os.path.join(
    os.environ.get("WIKI_LOG_DIR") or os.path.expanduser("~/Library/Logs/wiki-starter"), "runner.log"
)
ROTATE_BYTES = 5 * 1024 * 1024


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def emit(etype, **fields):
    """Append one event under an exclusive lock (the runner and the viewer both write)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    rec = {"t": now(), "type": etype}
    rec.update({k: v for k, v in fields.items() if v not in (None, "")})
    if not rec.get("run"):
        run = os.environ.get("WIKI_RUN_ID")
        if run:
            rec["run"] = run
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with open(EVENTS + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if os.path.exists(EVENTS) and os.path.getsize(EVENTS) > ROTATE_BYTES:
                os.replace(EVENTS, EVENTS[: -len(".jsonl")] + ".1.jsonl")
            with open(EVENTS, "a", encoding="utf-8") as f:
                f.write(line)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
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
    """cache/md/_intake/a/b.pdf.md -> raw/_intake/a/b.pdf (the converter's naming)."""
    if path.startswith("cache/md/") and path.endswith(".md"):
        return "raw/" + path[len("cache/md/") : -len(".md")]
    return ""


# ------------------------------------------------------------- claude stream -------
class ClaudeStream:
    """Turns Claude Code stream-json messages into events. Tolerates unknown shapes."""

    def __init__(self, label):
        self.label = label
        self.pending = {}  # tool_use id -> (name, input)
        self.current = ""  # the document or packet being worked on
        self.done = False

    def handle(self, msg):
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        content = (msg.get("message") or {}).get("content")
        if kind == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.pending[block.get("id")] = (block.get("name"), block.get("input") or {})
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

    def on_tool(self, name, inp):
        if name == "Read":
            path = rel(inp.get("file_path", ""))
            src = mirror_to_source(path)
            if src.startswith("raw/_intake/"):
                self.set_current(src)
            elif path.startswith("raw/inbox/") and path.endswith((".md", ".markdown")):
                self.set_current(path)
        elif name in ("Write", "Edit", "MultiEdit"):
            path = rel(inp.get("file_path", ""))
            if path.startswith("wiki/") or path in ("index.md", "log.md"):
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


def follow(path, pid, label, poll=0.5):
    stream = ClaudeStream(label)
    buf, offset = "", 0
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
                except (ValueError, TypeError):
                    log(f"claude {label}: {line[:500]}")
        elif not alive:
            if buf.strip():
                try:
                    stream.handle(json.loads(buf))
                except (ValueError, TypeError):
                    log(f"claude {label}: {buf.strip()[:500]}")
            break
        if not chunk:
            time.sleep(poll)
    if not stream.done:
        emit("claude-end", label=label, ok=False, detail="stopped")


# ----------------------------------------------------------- convert stream --------
def ignored_name(name):
    """The runner's rule (is_ignored_name in wiki_runner.sh): files it never processes."""
    return (name.startswith((".", "Icon", "~$")) or name == "README.md"
            or name.endswith((".download", ".crdownload", ".part", ".partial", ".tmp")))


def convert_stream(stream=sys.stdin):
    for line in stream:
        log(line.rstrip("\n"))
        s = line.strip()
        if s.startswith("[") and "] " in s:
            head, _, name = s.partition("] ")
            if head[1:].isdigit() and name and not ignored_name(os.path.basename(name)):
                emit("convert", file="raw/" + name)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wiki activity log.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("emit")
    e.add_argument("type")
    e.add_argument("fields", nargs="*")
    c = sub.add_parser("claude-stream")
    c.add_argument("--file", required=True)
    c.add_argument("--pid", type=int, required=True)
    c.add_argument("--label", default="")
    sub.add_parser("convert-stream")
    a = ap.parse_args(argv)
    if a.cmd == "emit":
        fields = {}
        for kv in a.fields:
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
        emit(a.type, **fields)
    elif a.cmd == "claude-stream":
        follow(a.file, a.pid, a.label)
    else:
        convert_stream()


if __name__ == "__main__":
    main()
