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

On Nucleus Cloud ("engine": "credits" in wiki.config.json, with WIKI_CREDITS_URL and
WIKI_CREDITS_TOKEN set) there is no claude command: each question goes instead to the
credits service's /v1/step, as one message with no tools, the schema written into the
system prompt and the answer read back as JSON. A picture the question names inside
read_dirs goes with it as an image; nothing else on the computer is sent.
"""
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

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


# ---------------------------------------------------------------- credits ----------
CREDITS_KIND = "wiki"           # the step's kind on the credits service (its price)
SCHEMA_NOTE = ("\n\n---\nYou have no tools. A picture named in the question, if any, is attached to it. "
               "Answer with one JSON object and nothing else, matching this JSON schema:\n")
PICTURE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                 ".webp": "image/webp"}
PICTURE_MAX_BYTES = 5_000_000   # the service takes about 7 MB of base64 per picture


def credits_service():
    """(url, token) when this wiki reads through a credits service (Nucleus Cloud), else None."""
    url = (os.environ.get("WIKI_CREDITS_URL") or "").strip().rstrip("/")
    token = (os.environ.get("WIKI_CREDITS_TOKEN") or "").strip()
    return (url, token) if url and token else None


def pictures_named(user, read_dirs):
    """The pictures in read_dirs whose path the question names: what Claude would have
    opened with Read. At most two, each small enough for the service."""
    found = []
    for d in read_dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            path = os.path.join(os.path.abspath(d), name)
            mime = PICTURE_TYPES.get(os.path.splitext(name)[1].lower())
            if mime and path in user and os.path.isfile(path) and os.path.getsize(path) <= PICTURE_MAX_BYTES:
                found.append((path, mime))
    return found[:2]


def json_in(text):
    """The JSON object in a reply: the whole text, a fenced block, or from its first { to
    its last }."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    for cand in (text, m.group(1) if m else "", text[text.find("{"): text.rfind("}") + 1]):
        try:
            v = json.loads(cand)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    return None


def credits_model(model):
    """The credits service's short model name for the one asked for."""
    return "haiku" if "haiku" in (model or "").lower() else "sonnet"


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
        # The same, per kind of call ("read", "overview", "other"), with the tokens Claude
        # reported: what the run's summary shows, so a cost can be traced to its calls.
        self.kinds = {}
        self.no_effort = False  # this claude does not know --effort (an older CLI)
        self.deadline = None   # local_engine.Model's interface: a per-call limit is used instead
        self.credits = credits_service()

    def ask(self, sysmsg, user, schema, max_tokens=None, read_dirs=(), kind="other", model=None, effort=None):
        """kind names the call in the run's summary; model and effort, when given, are this
        call's own (an effort is never sent to Haiku, which has no such setting)."""
        model = model or self.model
        effort = effort if effort and not self.no_effort and "haiku" not in (model or "").lower() else None
        try:
            return self._ask(sysmsg, user, schema, read_dirs, kind, model, effort)
        except (RuntimeError, ClaudeUnavailable) as e:
            # An older claude refuses the flag before it starts ("error: unknown option
            # '--effort'"): the call is asked again without it, and so is every later one.
            said = str(e).lower()
            if effort and "--effort" in said and ("unknown option" in said or "unknown argument" in said):
                with self.lock:
                    self.no_effort = True
                return self._ask(sysmsg, user, schema, read_dirs, kind, model, None)
            raise

    def _ask(self, sysmsg, user, schema, read_dirs, kind, model, effort):
        if self.credits:
            return self._ask_credits(sysmsg, user, schema, read_dirs, kind, model)
        cmd = [self.bin, "-p", "--output-format", "json", "--no-session-persistence",
               "--setting-sources", "", "--strict-mcp-config",
               "--system-prompt", sysmsg, "--json-schema", json.dumps(loose(schema)),
               "--max-budget-usd", f"{self.budget:.2f}"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if read_dirs:
            # Read, and only inside these folders ("//" marks an absolute path in a rule).
            rules = ",".join(f"Read(/{os.path.abspath(d)}/**)" for d in read_dirs)
            cmd += ["--tools", "Read", "--allowedTools", rules, "--permission-mode", "dontAsk"]
            for d in read_dirs:
                cmd += ["--add-dir", d]
        else:
            cmd += ["--tools", ""]
        # Haiku thinks at length before a structured answer unless told not to: in a real run
        # nine tenths of a Haiku read's output was hidden thinking, billed as output, which made
        # it dearer than Sonnet's read. Haiku has no effort setting, so its thinking is off.
        env = dict(os.environ, MAX_THINKING_TOKENS="0") if "haiku" in (model or "").lower() else None
        t0 = time.time()
        with self.lock:
            if self.stopped:
                raise ClaudeUnavailable("the run is stopping")
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                                     stderr=subprocess.PIPE, text=True, cwd=self.cwd, start_new_session=True)
            except FileNotFoundError:
                raise ClaudeUnavailable(f"the {self.bin} command is not installed")
            self.running.add(p)
        try:
            out, err = p.communicate(user, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self._kill(p)
            self._count(time.time() - t0, None, failed=True, kind=kind)
            raise RuntimeError(f"Claude gave no answer within {self.timeout:.0f}s")
        finally:
            with self.lock:
                self.running.discard(p)
        rec = answer_of(out)
        cost = (rec or {}).get("total_cost_usd")
        usage = (rec or {}).get("usage")
        if rec is None or rec.get("is_error") or rec.get("subtype") not in (None, "success"):
            self._count(time.time() - t0, cost, failed=True, kind=kind, usage=usage)
            said = str((rec or {}).get("result") or "").strip() or (err or "").strip()
            status = (rec or {}).get("api_error_status")
            server = isinstance(status, int) and not isinstance(status, bool) and status >= 500
            if (self.stopped or server or unavailable_words(said)
                    or (rec is None and not (out or "").strip())):
                raise ClaudeUnavailable(said[-300:] or f"claude exited {p.returncode}")
            # Its start ("API Error: ...", read by the reader's fallback) and its end.
            said = said if len(said) <= 300 else f"{said[:120]} ... {said[-170:]}"
            raise RuntimeError(f"Claude: {(rec or {}).get('subtype') or 'no answer'}: {said}")
        self._count(time.time() - t0, cost, kind=kind, usage=usage)
        answer = rec.get("structured_output")
        if not isinstance(answer, dict):
            try:
                answer = json.loads(rec.get("result") or "")
            except (TypeError, ValueError):
                answer = None
        if not isinstance(answer, dict):
            raise RuntimeError("Claude's answer was not in the asked form")
        return shaped(answer, schema)

    def _ask_credits(self, sysmsg, user, schema, read_dirs, kind, model):
        """One question on the credits service (Nucleus Cloud). Out of credits, signed out,
        or the service out of reach stops the run (ClaudeUnavailable, no try counted); a
        refusal of this question (HTTP 400) or an answer not in the asked form counts
        against the document, as with claude -p."""
        url, token = self.credits
        content = [{"type": "text", "text": user}]
        for path, mime in pictures_named(user, read_dirs):
            with open(path, "rb") as fh:
                content.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                             "data": base64.b64encode(fh.read()).decode("ascii")}})
        body = {"kind": CREDITS_KIND, "model": credits_model(model),
                "system": sysmsg + SCHEMA_NOTE + json.dumps(loose(schema)),
                "messages": [{"role": "user", "content": content}]}
        req = urllib.request.Request(url + "/v1/step", data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "wiki-starter",
                                              "Authorization": "Bearer " + token})
        t0 = time.time()
        with self.lock:
            if self.stopped:
                raise ClaudeUnavailable("the run is stopping")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ans = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            self._count(time.time() - t0, None, failed=True, kind=kind)
            try:
                said = str(json.loads(e.read().decode("utf-8", "replace")).get("error") or "")[:300]
            except (ValueError, AttributeError, OSError):
                said = ""
            if e.code == 402:
                raise ClaudeUnavailable("credit balance is too low: top up Nucleus credits")
            if e.code in (401, 403):
                raise ClaudeUnavailable("not signed in to Nucleus credits")
            if e.code >= 500 or e.code in (408, 429):
                raise ClaudeUnavailable(f"the credits service failed (HTTP {e.code}) {said}".strip())
            raise RuntimeError(f"Claude: the credits service refused the question (HTTP {e.code}): {said}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._count(time.time() - t0, None, failed=True, kind=kind)
            raise ClaudeUnavailable(f"can't reach the credits service: {getattr(e, 'reason', e)}")
        charged = ans.get("charged") if isinstance(ans, dict) else None
        cost = charged * 0.01 if isinstance(charged, (int, float)) and not isinstance(charged, bool) else None
        answer = json_in(ans.get("text") if isinstance(ans, dict) else "")
        if not isinstance(answer, dict):
            self._count(time.time() - t0, cost, failed=True, kind=kind)
            raise RuntimeError("Claude's answer was not in the asked form")
        self._count(time.time() - t0, cost, kind=kind)
        return shaped(answer, schema)

    def spent(self):
        with self.lock:
            return self.stats["cost"]

    def _count(self, seconds, cost, failed=False, kind="other", usage=None):
        with self.lock:
            k = self.kinds.setdefault(kind, {"calls": 0, "cost": 0.0, "seconds": 0.0, "input": 0,
                                             "cache_read": 0, "cache_write": 0, "output": 0})
            for d in (self.stats, k):
                d["calls"] += 1
                d["seconds"] += seconds
                if isinstance(cost, (int, float)):
                    d["cost"] += float(cost)
            self.stats["failed"] += 1 if failed else 0
            if isinstance(cost, (int, float)):
                self.stats["costed"] += 1
            usage = usage if isinstance(usage, dict) else {}
            for name, field in (("input", "input_tokens"), ("cache_read", "cache_read_input_tokens"),
                                ("cache_write", "cache_creation_input_tokens"), ("output", "output_tokens")):
                v = usage.get(field)
                if isinstance(v, int) and not isinstance(v, bool):
                    k[name] += v

    def summary(self):
        """One line per kind of call: calls, cost, time and tokens (input, read from the
        cache, written to it, output), for the runner log."""
        with self.lock:
            return "; ".join(f"{kind} {k['calls']} calls ${k['cost']:.4f} {k['seconds']:.0f}s "
                             f"in {k['input']} cached {k['cache_read']} cache-write {k['cache_write']} out {k['output']}"
                             for kind, k in sorted(self.kinds.items()))

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
