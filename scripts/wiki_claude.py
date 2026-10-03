#!/usr/bin/env python3
"""wiki_claude.py — one question to Claude, answered in JSON. The fast pipeline's reader.

With Claude, fast reading is the default ("pipeline" in wiki.config.json: "fast", or
"classic" for a Claude session per batch): the wiki is built the way the model on this Mac
builds it (scripts/local_engine.py), with Claude reading:

  1. read: one call per document answers one JSON schema: what the document is, who it
     names, every figure and identifier, how the parties relate, the topic pages it
     informs, its project folder, and for a photo what the picture shows. Many documents
     are read at once;
  2. check and write: code keeps only what the document contains, records the figures in
     archive/claims.jsonl, finds contradictions (each becomes a question in the Review tab,
     for the owner or Jev), writes the pages, index.md and log.md, and files the original;
  3. overviews: one call per page the run touched writes its short overview, many at once.

Claude never writes a file and has no tools, except Read, confined to one folder, for a
picture or a scan it has to look at. Each call is `claude -p` with this pipeline's own
system prompt and --json-schema, run from an empty folder outside the wiki (no project
CLAUDE.md), with no settings files and no MCP servers, signed in the way the runner signed
in (an API key or a Claude plan). Standard library only.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

# A failure in these words means Claude itself cannot run now (sign-in, billing, the
# plan's usage limit, a Mac that is offline, Anthropic's service failing), not that one
# document is at fault: the run stops, and no file counts a try. Matched against Claude's
# own message, never against the whole record. Never a per-document error (a 4xx such as
# "prompt is too long"): such a document counts a try and is set aside after two.
UNAVAILABLE = ("failed to authenticate", "invalid api key", "api key is invalid", "not logged in",
               "please run /login", "oauth token", "credit balance", "unauthorized",
               "usage limit", "limit reached", "rate limit", "rate_limit", "overloaded",
               # no connection (an offline Mac, Wi-Fi lost, DNS, a proxy), as the CLI words it
               "api error: connection", "connection error", "connection refused", "econnrefused",
               "enotfound", "econnreset", "etimedout", "eai_again", "can't reach the api server",
               "unable to connect", "socket hang up", "network error", "fetch failed",
               "request timed out",
               # the service failing (any 5xx)
               "internal server error", "service unavailable", "bad gateway", "gateway timeout")
# "API Error: <status>" in the CLI's own result: a server-side status (5xx), a timeout
# (408) or a rate limit (429) is the service, any other 4xx the document.
API_ERROR = re.compile(r"api error:\s*(\d{3})?", re.I)


def unavailable_words(text):
    """True when Claude's message says the service cannot be used now (see UNAVAILABLE)."""
    low = (text or "").lower()
    if any(w in low for w in UNAVAILABLE):
        return True
    m = API_ERROR.search(low)
    return bool(m and m.group(1) and (m.group(1).startswith("5") or m.group(1) in ("408", "429")))


def service_failure(text):
    """True for an API failure the CLI reported that is not about the document: "API
    Error:" with no status (a transport failure in words not listed above), or a status
    that is the service's (5xx, 408, 429). Used for the reader's fallback: failure after
    failure of this kind, with nothing answered in the run, is Claude, not the documents."""
    m = API_ERROR.search(text or "")
    if not m:
        return False
    return not m.group(1) or m.group(1).startswith("5") or m.group(1) in ("408", "429")


class ClaudeUnavailable(Exception):
    """Claude could not run at all; no document is to blame."""


def loose(schema):
    """The schema without length and count limits: code trims every answer anyway, and a
    limit only gives an otherwise good answer a way to fail validation."""
    if isinstance(schema, dict):
        return {k: loose(v) for k, v in schema.items() if k not in ("maxLength", "maxItems", "minItems")}
    if isinstance(schema, list):
        return [loose(v) for v in schema]
    return schema


def shaped(value, schema):
    """The answer with every field of the type the schema asks for (a list where a list is
    asked for, text where text is): an answer that skipped validation cannot hand the code
    a string to walk letter by letter."""
    kind = (schema or {}).get("type")
    if kind == "object":
        value = value if isinstance(value, dict) else {}
        props = schema.get("properties") or {}
        return {k: shaped(value.get(k), s) for k, s in props.items()}
    if kind == "array":
        items = schema.get("items") or {}
        return [shaped(v, items) for v in value if items.get("type") != "object" or isinstance(v, dict)] \
            if isinstance(value, list) else []
    if kind == "string":
        return value if isinstance(value, str) else ""
    if kind in ("number", "integer"):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    if kind == "boolean":
        return bool(value)
    return value


def answer_of(stdout):
    """The result record of `claude -p --output-format json` (the last JSON line, for a
    CLI that prints more than one)."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("type", "result") == "result":
                return rec
    return None


class Claude:
    """`claude -p` as a function: a system prompt, a question and a JSON schema in, the
    answer out. Thread-safe; keeps the calls, cost and time for the run's summary, and
    stops every call still running when the run stops."""

    def __init__(self, model="", budget=1.0, timeout=900):
        self.bin = os.environ.get("CLAUDE_BIN") or "claude"
        self.model, self.budget, self.timeout = model, budget, timeout
        self.cwd = tempfile.mkdtemp(prefix="wiki-claude-")
        self.lock = threading.Lock()
        self.running = set()
        self.stopped = False
        self.stats = {"calls": 0, "failed": 0, "cost": 0.0, "seconds": 0.0, "costed": 0}
        self.deadline = None   # local_engine.Model's interface: a per-call limit is used instead

    def ask(self, sysmsg, user, schema, max_tokens=None, read_dirs=()):
        cmd = [self.bin, "-p", "--output-format", "json", "--no-session-persistence",
               "--setting-sources", "", "--strict-mcp-config",
               "--system-prompt", sysmsg, "--json-schema", json.dumps(loose(schema)),
               "--max-budget-usd", f"{self.budget:.2f}"]
        if self.model:
            cmd += ["--model", self.model]
        if read_dirs:
            # Read, and only inside these folders ("//" marks an absolute path in a rule).
            rules = ",".join(f"Read(/{os.path.abspath(d)}/**)" for d in read_dirs)
            cmd += ["--tools", "Read", "--allowedTools", rules, "--permission-mode", "dontAsk"]
            for d in read_dirs:
                cmd += ["--add-dir", d]
        else:
            cmd += ["--tools", ""]
        t0 = time.time()
        with self.lock:
            if self.stopped:
                raise ClaudeUnavailable("the run is stopping")
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, cwd=self.cwd, start_new_session=True)
            except FileNotFoundError:
                raise ClaudeUnavailable(f"the {self.bin} command is not installed")
            self.running.add(p)
        try:
            out, err = p.communicate(user, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self._kill(p)
            self._count(time.time() - t0, None, failed=True)
            raise RuntimeError(f"Claude gave no answer within {self.timeout:.0f}s")
        finally:
            with self.lock:
                self.running.discard(p)
        rec = answer_of(out)
        cost = (rec or {}).get("total_cost_usd")
        if rec is None or rec.get("is_error") or rec.get("subtype") not in (None, "success"):
            self._count(time.time() - t0, cost, failed=True)
            said = str((rec or {}).get("result") or "").strip() or (err or "").strip()
            status = (rec or {}).get("api_error_status")
            server = isinstance(status, int) and not isinstance(status, bool) and status >= 500
            if (self.stopped or server or unavailable_words(said)
                    or (rec is None and not (out or "").strip())):
                raise ClaudeUnavailable(said[-300:] or f"claude exited {p.returncode}")
            # Its start ("API Error: ...", read by the reader's fallback) and its end.
            said = said if len(said) <= 300 else f"{said[:120]} ... {said[-170:]}"
            raise RuntimeError(f"Claude: {(rec or {}).get('subtype') or 'no answer'}: {said}")
        self._count(time.time() - t0, cost)
        answer = rec.get("structured_output")
        if not isinstance(answer, dict):
            try:
                answer = json.loads(rec.get("result") or "")
            except (TypeError, ValueError):
                answer = None
        if not isinstance(answer, dict):
            raise RuntimeError("Claude's answer was not in the asked form")
        return shaped(answer, schema)

    def spent(self):
        with self.lock:
            return self.stats["cost"]

    def _count(self, seconds, cost, failed=False):
        with self.lock:
            self.stats["calls"] += 1
            self.stats["failed"] += 1 if failed else 0
            self.stats["seconds"] += seconds
            if isinstance(cost, (int, float)):
                self.stats["cost"] += float(cost)
                self.stats["costed"] += 1

    @staticmethod
    def _kill(p):
        """Stop a call: claude and anything it started (its own process group), politely
        first; whatever ignores that for 10 seconds is killed. Never waits longer than
        that, so a call that will not stop cannot hold its lane for ever."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sig)
            except OSError:
                pass
            try:
                p.communicate(timeout=10)
                return
            except (subprocess.TimeoutExpired, ValueError, OSError):
                continue   # ValueError: another thread is reading its output; it ends there

    def stop(self):
        """No new calls; the ones still running are stopped."""
        with self.lock:
            self.stopped = True
            running = list(self.running)
        for p in running:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except OSError:
                    pass
                try:
                    p.wait(timeout=10)   # its own thread collects the output
                    break
                except subprocess.TimeoutExpired:
                    continue
        shutil.rmtree(self.cwd, ignore_errors=True)
