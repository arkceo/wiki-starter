#!/usr/bin/env python3
"""local_engine.py — build the wiki with a model that runs on this Mac.

Used instead of Claude when wiki.config.json says "engine": "local". Nothing leaves the
Mac: the model runs in llama.cpp's llama-server, started for the run on 127.0.0.1 and
stopped afterwards, so its memory is free whenever the wiki is idle.

A small local model is good at reading one document and answering narrow questions; it
is not reliable at editing many files on its own. So this script does the steps and the
model only answers, in JSON forced to match a schema:

  documents (raw/_intake/, already converted to cache/md/ by the runner)
    1. read the Markdown mirror in parts that fit the model's context;
    2. per part: title, type, date, summary points, facts, companies and people,
       identifiers, decisions, open questions;
    3. choose the project folder among those that exist;
    4. check new facts against the existing pages of the companies and people named;
    5. only then write: the summary page (wiki/sources/), the pages of the companies and
       people (wiki/companies/), the project page; then file the original under
       raw/<project>/, unrenamed unless the name is taken.
    An identifier (registration or tax number, ...) is kept only if it appears in the
    document word for word; a conflict goes to wiki/_review.md instead of being written
    over.
  Update Packets (raw/inbox/)
    parsed by code; one page per packet (wiki/decisions/ or wiki/updates/), a dated entry
    on every affected page (the model only maps page names it cannot match), review
    entries for "Supersedes" and "Open questions"; the packet moves to archive/inbox/. A
    plain note without the packet header is applied as one.

Existing pages are only appended to, never rewritten. Frontmatter, wikilinks, index.md
and log.md are written by code, and every string that came from a document or the model
is escaped, so neither a model mistake nor a hostile document can break the site or put
markup in it.

Usage:
  local_engine.py run [--server URL]   process everything waiting
  local_engine.py run --docs FILE      only the documents listed in FILE (one batch; the
                                       runner calls this), no Update Packets
  local_engine.py run --packets        only the Update Packets
  local_engine.py check                start the model and ask one question (installer)

Exit status: 0 all done; 1 some files failed (they stay queued and are retried, then set
aside, like any file); 2 the model could not run at all (nothing is counted against the
files).

Standard library only. Progress goes to the activity log (scripts/wiki_events.py), and
each step (loading the model, each question, writing, filing) to its live line, with the
model's answer streamed so the line can count the words as they arrive.
"""
import argparse
import datetime
import filecmp
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WIKI_DIR, "scripts"))
import wiki_events  # noqa: E402
import wiki_netguard  # noqa: E402

CHUNK_CHARS = 18000          # about 5-6k tokens: leaves room for instructions and the answer
MAX_PARTS = 24               # longer documents are summarised from their first parts
DOC_BUDGET_SECONDS = 2700    # one document may take this long before it counts as failed
TOKEN_NOTE_SECONDS = 1.5     # how often the live line hears how far an answer has got
HOUSE_RULES_CHARS = 2500
MAX_PAGE_CHOICES = 150
IGNORED_DIRS = {"_intake", "inbox", "_needs-review", "_unfiled"}
DOC_TYPES = ["contract", "agreement", "invoice", "quotation", "purchase-order", "receipt",
             "statement", "report", "minutes", "letter", "email", "policy", "procedure",
             "form", "presentation", "spreadsheet", "brochure", "certificate", "other"]
ID_KINDS = ["registered_name", "registration_no", "tin_no", "sst_no", "licence_no",
            "bank_account"]


class ModelUnavailable(Exception):
    """The model could not be started or stopped answering: the run stops, and no file
    is counted as failed."""


class DocTimeout(Exception):
    """One document took longer than its budget: it counts as a failed attempt."""


def today():
    return datetime.date.today().isoformat()


def log(msg):
    wiki_events.log(f"local: {msg}")


# =================================================================== escaping ====
def clean_line(text, limit=400):
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


MD_SPECIAL = re.compile(r"([\\`*_\[\]()!#|~])")


def esc(text, limit=400):
    """Text from a document or the model, for a page body: one line, no HTML, and no
    Markdown syntax (links, images, code), so it can only ever show as text."""
    t = clean_line(text, limit).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return MD_SPECIAL.sub(r"\\\1", t)


def label(text, limit=120):
    """Text for a link label or a page title: one line, no brackets, bars or angle
    brackets (wikilink labels and the site's search results are rendered as HTML)."""
    t = clean_line(text, limit)
    for a, b in (("[", "("), ("]", ")"), ("|", "/"), ("<", "‹"), (">", "›"), ("`", "'")):
        t = t.replace(a, b)
    return t


def owner_text(text):
    """Text the owner wrote (an Update Packet): Markdown kept, raw HTML and script links not."""
    t = str(text or "").replace("<", "&lt;")
    return re.sub(r"\]\(\s*(?:javascript|data|vbscript):", "](#", t, flags=re.I)


def yq(value):
    """A YAML scalar, always double-quoted (frontmatter written by code never breaks)."""
    return json.dumps(str(value), ensure_ascii=False)


def raw_link(path):
    """A Markdown link to an original, served read-only by the viewer."""
    return f"[{label(os.path.basename(path))}](/{urllib.parse.quote(path)})"


def slugify(text, limit=60):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:limit].rstrip("-")
    if not t:  # a name in another script: keep it stable and unique
        t = "page-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return t


SUFFIX_WORDS = ("sdn bhd", "berhad", "bhd", "pte ltd", "pte", "ltd", "limited", "inc", "llc",
                "llp", "plc", "corp", "corporation", "company", "co", "gmbh", "ag", "sa", "pty ltd",
                "pty")
SUFFIX_RE = re.compile(r"(?:\s+(?:" + "|".join(re.escape(w) for w in SUFFIX_WORDS) + r"))+\s*$")
# The same suffixes as written ("Sdn. Bhd.", ", Inc."), to derive the short alias.
SUFFIX_ORIG_RE = re.compile(r"(?:[\s,]+(?:" + "|".join(w.replace(" ", r"\.?\s*") + r"\.?" for w in SUFFIX_WORDS)
                            + r"))+\s*$", re.I)


def norm_full(name):
    """Casefolded words, any script, punctuation dropped."""
    n = unicodedata.normalize("NFKC", str(name or "")).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[\W_]+", " ", n).split())


def split_suffix(name):
    """('northwind trading', 'sdn bhd') for 'Northwind Trading Sdn. Bhd.'"""
    full = norm_full(name)
    m = SUFFIX_RE.search(" " + full)
    if not m:
        return full, ""
    core = (" " + full)[: m.start()].strip()
    return (core, m.group(0).strip()) if core else (full, "")


def same_entity(a, b):
    """Two names for one company: identical, or the same core where at most one carries
    a legal suffix ('Northwind Trading' and 'Northwind Trading Sdn Bhd'). Different
    suffixes ('... Sdn Bhd' and '... Pte Ltd') are different companies."""
    if not a or not b:
        return False
    fa, fb = norm_full(a), norm_full(b)
    if fa and fa == fb:
        return True
    (ca, sa), (cb, sb) = split_suffix(a), split_suffix(b)
    return bool(ca) and ca == cb and (not sa or not sb)


# ================================================================ frontmatter ====
def split_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            nl = text.find("\n", end + 4)
            return text[4:end], text[(nl + 1) if nl != -1 else len(text):]
    return "", text


def _key_block(fm, key):
    """(start, end, first-line value) of a top-level key and its continuation lines."""
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", fm, re.M)
    if not m:
        return None
    end = m.end()
    for line in fm[m.end():].split("\n")[1:]:
        if re.match(r"^([ \t]+\S|-[ \t]|-$)", line):
            end += 1 + len(line)
        else:
            break
    return m.start(), end, m.group(1).strip()


def _flow_items(inner):
    """Items of a YAML flow list body, honouring quotes (a comma inside "..." stays)."""
    items, cur, q, i = [], "", None, 0
    while i < len(inner):
        ch = inner[i]
        if q:
            if ch == "\\" and q == '"' and i + 1 < len(inner):
                cur += ch + inner[i + 1]
                i += 2
                continue
            cur += ch
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur += ch
        elif ch == ",":
            items.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    items.append(cur)
    out = []
    for it in items:
        it = it.strip()
        if not it:
            continue
        if it[0] == '"' and it[-1:] == '"':
            try:
                it = json.loads(it)
            except ValueError:
                it = it[1:-1]
        elif it[0] == "'" and it[-1:] == "'":
            it = it[1:-1].replace("''", "'")
        out.append(it)
    return out


def fm_list(fm, key):
    """A list value in any common shape: [a, "b, c"], block items (indented or not), or a
    single scalar. None when the shape is not a list of plain values (left alone)."""
    blk = _key_block(fm, key)
    if blk is None:
        return []
    start, end, first = blk
    if first.startswith("["):
        if not first.endswith("]"):
            return None
        return _flow_items(first[1:-1])
    rest = fm[start:end].split("\n")[1:]
    if not first:
        items = []
        for line in rest:
            m = re.match(r"^[ \t]*-[ \t]*(.*)$", line)
            if not m:
                return None  # a nested map or multi-line value: not ours to rewrite
            items += _flow_items(m.group(1)) if m.group(1) else []
        return items
    if rest or first.startswith(("{", "|", ">")):
        return None
    return _flow_items(first)


def fm_get(fm, key):
    blk = _key_block(fm, key)
    if not blk:
        return ""
    v = blk[2]
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        items = _flow_items(v)
        return items[0] if items else ""
    return v


def fm_set(fm, key, line):
    """Replace a top-level key's whole entry (with its continuation lines), or add it."""
    blk = _key_block(fm, key)
    if blk:
        return fm[: blk[0]] + line + fm[blk[1]:]
    return fm.rstrip("\n") + "\n" + line


def touch_page(path, add_source=None, status=None):
    """Update `updated:` (and optionally sources/status) in an existing page's frontmatter."""
    text = read(path)
    fm, body = split_frontmatter(text)
    if not fm:
        return
    fm = fm_set(fm, "updated", f"updated: {today()}")
    if add_source:
        srcs = fm_list(fm, "sources")
        if srcs is not None and add_source not in srcs:
            fm = fm_set(fm, "sources", "sources: [" + ", ".join(yq(s) for s in srcs + [add_source]) + "]")
    if status:
        fm = fm_set(fm, "status", f"status: {status}")
    write(path, "---\n" + fm.strip("\n") + "\n---\n" + body)


def read(path):
    with open(os.path.join(WIKI_DIR, path), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def write(path, text):
    full = os.path.join(WIKI_DIR, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".local-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, full)


def append_under(path, heading, lines):
    """Append bullet lines under a '## heading' section (created at the end if missing)."""
    text = read(path).rstrip("\n") + "\n"
    block = "".join(l + "\n" for l in lines)
    m = re.search(rf"^## {re.escape(heading)}\s*$", text, re.M)
    if not m:
        text += f"\n## {heading}\n\n" + block
    else:
        nxt = re.search(r"^## ", text[m.end():], re.M)
        at = m.end() + nxt.start() if nxt else len(text)
        head, tail = text[:at].rstrip("\n") + "\n", text[at:]
        text = head + block + ("\n" + tail if tail else "")
    write(path, text)


def chunks_of(body):
    paras = re.split(r"\n\s*\n", body)
    parts, cur = [], ""
    for p in paras:
        while len(p) > CHUNK_CHARS:  # one huge paragraph (a table, OCR without breaks)
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(p[:CHUNK_CHARS])
            p = p[CHUNK_CHARS:]
        if len(cur) + len(p) + 2 > CHUNK_CHARS and cur:
            parts.append(cur)
            cur = ""
        cur = (cur + "\n\n" + p) if cur else p
    if cur.strip():
        parts.append(cur)
    return parts


# ===================================================================== files =====
def same_file(a, b):
    try:
        return filecmp.cmp(os.path.join(WIKI_DIR, a), os.path.join(WIKI_DIR, b), shallow=False)
    except OSError:
        return False


def choose_dest(folder, name, src):
    """Where src will be filed: the name itself, or 'name (2).ext', ... — the first slot
    that is free or already holds this very file. The same on every retry."""
    stem, ext = os.path.splitext(name)
    for n in range(1, 1000):
        dest = f"{folder}/{name}" if n == 1 else f"{folder}/{stem} ({n}){ext}"
        full = os.path.join(WIKI_DIR, dest)
        if not os.path.exists(full) or (os.path.isfile(full) and same_file(src, dest)):
            return dest
    raise RuntimeError(f"too many files named {name} in {folder}")


def place(src, dest):
    """Move src to dest without ever replacing a different file."""
    full_src, full_dest = os.path.join(WIKI_DIR, src), os.path.join(WIKI_DIR, dest)
    os.makedirs(os.path.dirname(full_dest), exist_ok=True)
    try:
        os.link(full_src, full_dest)
    except FileExistsError:
        if not same_file(src, dest):
            raise RuntimeError(f"{dest} appeared meanwhile; trying again next run")
    os.unlink(full_src)


# ===================================================================== model =====
class Model:
    """A llama-server on 127.0.0.1: either one we start on first use (and stop), or a
    given URL. An Update Packet usually needs no model at all, so it is not loaded until a
    question is asked."""

    def __init__(self, url=None, cfg=None):
        self.cfg = cfg or {}
        self.url = url
        self.external = bool(url)
        self.proc = None
        self.deadline = None  # per-document budget, set by the caller
        self.stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                      "prompt_ms": 0.0, "predict_ms": 0.0, "json_retries": 0, "json_failures": 0}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        model = os.path.expanduser(self.cfg.get("file") or "")
        if not model or not os.path.isfile(model):
            raise ModelUnavailable(f"local model not found: {model or '(not configured)'}")
        binary = self.cfg.get("server") or shutil.which("llama-server") or "/opt/homebrew/bin/llama-server"
        if not os.path.exists(binary) and not shutil.which(binary):
            raise ModelUnavailable("llama-server not found (brew install llama.cpp)")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        args = [binary, "-m", model, "--host", "127.0.0.1", "--port", str(port),
                "-c", str(int(self.cfg.get("ctx") or 32768)), "-np", "1",
                "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0",
                "-ngl", str(self.cfg.get("gpuLayers", 99)), "--jinja"]
        if self.cfg.get("threads"):
            args += ["-t", str(int(self.cfg["threads"]))]
        os.makedirs(os.path.dirname(wiki_events.LOG_FILE), exist_ok=True)
        logf = open(wiki_events.LOG_FILE, "a")
        log(f"starting the model ({os.path.basename(model)})")
        waiting = wiki_events.read_now()  # the step that needs the model, resumed once it is up
        say("load", f"loading {model_name(model)} into memory")
        t0 = time.time()
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf)
        except OSError as e:
            raise ModelUnavailable(f"llama-server could not start: {e}")
        url = f"http://127.0.0.1:{port}"
        deadline = time.time() + float(self.cfg.get("loadTimeoutSeconds") or 600)
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise ModelUnavailable(f"llama-server exited while loading (code {self.proc.returncode})")
            try:
                with urllib.request.urlopen(url + "/health", timeout=5) as r:
                    if r.status == 200:
                        self.url = url  # only a server that answered is used
                        log(f"model ready in {round(time.time() - t0)}s")
                        if waiting.get("phase") and waiting["phase"] != "load":
                            wiki_events.set_now(waiting["phase"], **{k: waiting[k] for k in
                                                ("file", "detail", "n", "of") if k in waiting})
                        return
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                pass
            time.sleep(1)
        raise ModelUnavailable("the model did not load in time")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            log("model stopped; its memory is free again")
        self.proc = None
        if not self.external:
            self.url = None

    def ask(self, system, user, schema, max_tokens=1500):
        """One question, answered as JSON that matches schema. Returns the parsed object."""
        if not self.url:
            self.start()
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "answer", "strict": True, "schema": schema}},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        last = None
        for attempt in range(2):
            if attempt:
                # A cut-off or malformed answer: once more, deterministic, with more room.
                self.stats["json_retries"] += 1
                body["temperature"] = 0.0
                body["max_tokens"] = min(int(body["max_tokens"] * 2), 6000)
            data = self._post("/v1/chat/completions", body)
            self.stats["calls"] += 1
            usage = data.get("usage") or {}
            self.stats["prompt_tokens"] += usage.get("prompt_tokens") or 0
            self.stats["completion_tokens"] += usage.get("completion_tokens") or 0
            t = data.get("timings") or {}
            self.stats["prompt_ms"] += t.get("prompt_ms") or 0
            self.stats["predict_ms"] += t.get("predicted_ms") or 0
            try:
                choice = data["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError("the answer was cut off")
                return json.loads(choice["message"]["content"])
            except (KeyError, IndexError, TypeError, ValueError) as e:
                last = e
        self.stats["json_failures"] += 1
        raise RuntimeError(f"the model did not answer in the expected format ({last})")

    def _post(self, path, body):
        if self.deadline and time.time() > self.deadline:
            raise DocTimeout("this document took too long")
        timeout = float(self.cfg.get("requestTimeoutSeconds") or 900)
        if self.deadline:
            timeout = max(30.0, min(timeout, self.deadline - time.time()))
        # Streamed, so the live line can count the answer as it is written. The answer is
        # put back together into the same shape a plain reply has.
        body = dict(body, stream=True, stream_options={"include_usage": True})
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if "text/event-stream" not in (r.headers.get("Content-Type") or ""):
                    return json.loads(r.read())  # a server that does not stream
                return self._read_stream(r, time.time() + timeout)
        except urllib.error.HTTPError as e:
            # The server answered: a problem with this request (too long, ...), not the model.
            raise RuntimeError(f"the model refused the request ({e.code}: {e.read()[:200]!r})")
        except (socket.timeout, TimeoutError):
            if self.proc is not None and self.proc.poll() is not None:
                raise ModelUnavailable("the model stopped while answering")
            raise DocTimeout("the model took too long to answer")
        except (urllib.error.URLError, ConnectionError, http.client.HTTPException, OSError) as e:
            raise ModelUnavailable(f"the model is not answering ({e})")

    def _read_stream(self, r, hard_deadline):
        """Server-sent chunks -> {"choices": [{"message", "finish_reason"}], "usage", "timings"}.
        A stream that keeps trickling still ends at the request's deadline."""
        content, finish, usage, timings = [], None, {}, {}
        tokens, noted = 0, time.time()
        for raw in r:
            if time.time() > hard_deadline:
                raise TimeoutError("the answer took too long")
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if chunk.get("error"):
                raise RuntimeError(f"the model refused the request ({str(chunk['error'])[:200]})")
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                    tokens += 1
                elif delta.get("reasoning_content"):
                    tokens += 1
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            usage = chunk.get("usage") or usage
            timings = chunk.get("timings") or timings
            if time.time() - noted >= TOKEN_NOTE_SECONDS:
                wiki_events.update_now(tokens=tokens)
                noted = time.time()
        if finish is None and self.proc is not None and self.proc.poll() is not None:
            raise ModelUnavailable("the model stopped while answering")
        return {"choices": [{"message": {"content": "".join(content)}, "finish_reason": finish}],
                "usage": usage, "timings": timings}


# ==================================================================== schemas ====
def S(t, **kw):
    d = {"type": t}
    d.update(kw)
    return d


def obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or list(props),
            "additionalProperties": False}


def text(n):
    return S("string", maxLength=n)


EXTRACT_SCHEMA = obj({
    "title": text(160),
    "doc_type": S("string", enum=DOC_TYPES),
    "doc_date": text(10),
    "summary": S("array", items=text(300), maxItems=8),
    "facts": S("array", maxItems=15, items=obj({"about": text(120), "fact": text(300)})),
    "entities": S("array", maxItems=12, items=obj({
        "name": text(120), "kind": S("string", enum=["company", "person", "product", "other"]),
        "role": text(160)})),
    "identifiers": S("array", maxItems=10, items=obj({
        "entity": text(120), "kind": S("string", enum=ID_KINDS), "value": text(80)})),
    "decisions": S("array", items=text(300), maxItems=8),
    "open_questions": S("array", items=text(300), maxItems=5),
})

CONDENSE_SCHEMA = obj({"title": text(160), "summary": S("array", items=text(300), maxItems=10)})

CONFLICT_SCHEMA = obj({"conflicts": S("array", maxItems=5, items=obj({
    "existing": text(240), "new": text(240), "explanation": text(240)}))})


def system_prompt(house_rules):
    rules = house_rules.strip()
    return (
        "You read documents for a small business's internal wiki. Report only what the text "
        "states. Never guess or add outside knowledge; leave a field empty when the text does "
        "not say. Keep names, amounts, dates and numbers exactly as written. Write short, plain "
        "sentences in the language of the document. Dates as YYYY-MM-DD when the text gives a "
        "full date."
        + ("\n\nWhat the business says about itself (its house rules):\n" + rules if rules else "")
    )


# ===================================================================== wiki ======
class Wiki:
    """What the code needs to know about existing pages."""

    def __init__(self):
        self.pages = {}  # wiki path -> {"title", "aliases", "tags"}
        for dirpath, dirs, files in os.walk(os.path.join(WIKI_DIR, "wiki")):
            dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
            for fn in files:
                if fn.endswith(".md") and not fn.startswith("_"):
                    rel = os.path.relpath(os.path.join(dirpath, fn), WIKI_DIR).replace(os.sep, "/")
                    self.add(rel)

    def add(self, rel):
        fm, _ = split_frontmatter(read(rel))
        self.pages[rel] = {"title": fm_get(fm, "title") or os.path.basename(rel)[:-3],
                           "aliases": fm_list(fm, "aliases") or [],
                           "tags": [t.lower() for t in (fm_list(fm, "tags") or [])]}

    def find_entity(self, name, kind):
        """The existing page of this company or person, if any. A person never matches a
        company page or the other way round."""
        exact, close = [], []
        for rel, p in self.pages.items():
            if not rel.startswith("wiki/companies/"):
                continue
            tags = set(p["tags"])
            if (kind == "person" and "company" in tags) or (kind == "company" and "person" in tags):
                continue
            names = [p["title"]] + p["aliases"]
            if any(norm_full(n) == norm_full(name) for n in names):
                exact.append(rel)
            elif kind == "company" and any(same_entity(n, name) for n in names):
                close.append(rel)
        if exact:
            return exact[0]
        return close[0] if len(close) == 1 else None

    def resolve(self, token):
        """A page named in an Update Packet ('companies/acme', 'overview', 'Acme Ltd')."""
        t = token.strip().strip("`*[]").strip()
        t = re.sub(r"^wiki/", "", t)
        t = re.sub(r"\.md$", "", t)
        if not t:
            return None
        for cand in (f"wiki/{t}.md", f"wiki/{t}/index.md"):
            if cand in self.pages:
                return cand
        base = slugify(t.split("/")[-1])
        hits = [r for r in self.pages if os.path.basename(r)[:-3] == base]
        if len(hits) == 1:
            return hits[0]
        hits = [r for r, p in self.pages.items() if norm_full(p["title"]) == norm_full(t)
                or (r.startswith("wiki/companies/") and same_entity(p["title"], t))]
        return hits[0] if len(hits) == 1 else None

    def link(self, rel, text=None):
        target = rel[len("wiki/"):-len(".md")]
        return f"[[{target}|{label(text or self.pages.get(rel, {}).get('title') or target)}]]"

    def has_link(self, page, rel):
        return f"[[{rel[len('wiki/'):-len('.md')]}|" in read(page)

    def unique_path(self, folder, slug, source=None):
        """wiki/<folder>/<slug>.md, or -2, -3... when a different source already owns it."""
        n = 1
        while True:
            rel = f"wiki/{folder}/{slug}{'' if n == 1 else '-' + str(n)}.md"
            if rel not in self.pages:
                return rel
            if source and source in (fm_list(split_frontmatter(read(rel))[0], "sources") or []):
                return rel  # the same document again (a retry): rewrite its own page
            n += 1


def projects():
    out = []
    root = os.path.join(WIKI_DIR, "raw")
    for d in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if os.path.isdir(os.path.join(root, d)) and d not in IGNORED_DIRS and not d.startswith("."):
            out.append(d)
    if "general" not in out:
        out.append("general")
    return out


class Run:
    current = ""  # the file being worked on, for events


def say(phase, detail="", **fields):
    """The Upload page's live line: what this file is going through right now."""
    wiki_events.set_now(phase, file=Run.current, detail=detail, **fields)


def model_name(path):
    """Qwen_Qwen3.5-9B-Q4_K_M.gguf -> Qwen3.5-9B, for people."""
    stem = re.sub(r"(?i)\.gguf$", "", os.path.basename(path or ""))
    stem = re.sub(r"(?i)[-_.](q\d\w*|iq\d\w*|f16|bf16)$", "", stem)
    stem = re.sub(r"^[A-Za-z]+_(?=[A-Za-z])", "", stem)  # the publisher's prefix
    stem = re.sub(r"(?i)-instruct\b", "", stem)
    stem = re.sub(r"(?i)-(\d+(?:\.\d+)?)b\b", lambda m: f"-{m.group(1)}B", stem)
    return stem or "the model"


def review(project, page, reason, lines):
    path = "wiki/_review.md"
    text_ = read(path).rstrip("\n") + "\n"
    first = f"- {esc(lines[0], 600)}\n" if lines else ""
    if first and first in text_:
        return  # already queued (a retry of the same file)
    text_ += f"\n## [{today()}] needs-review | {label(project, 60)} | {label(page, 160)} | {label(reason, 160)}\n"
    text_ += "".join(f"- {esc(l, 600)}\n" for l in lines)
    write(path, text_)
    wiki_events.emit("review", file=Run.current, page=path)


def log_line(op, project, title):
    path = "log.md"
    write(path, read(path).rstrip("\n") + "\n" + f"## [{today()}] {op:<11} | {label(project, 60)} | {label(title, 120)}\n")


def index_add(wiki, rel, section, summary):
    if wiki.has_link("index.md", rel):
        return
    append_under("index.md", section, [f"- {wiki.link(rel)} — {esc(summary, 160)}"])


def page_event(rel, created, wiki):
    wiki_events.emit("page", file=Run.current, page=rel, action="created" if created else "updated")
    wiki.add(rel)


# ================================================================== documents ====
def mirror_path(src):
    return "cache/md/" + src[len("raw/"):] + ".md"


def process_document(model, wiki, src, sysmsg):
    Run.current = src
    name = os.path.basename(src)
    mirror = mirror_path(src)
    if not os.path.isfile(os.path.join(WIKI_DIR, mirror)):
        # Not converted yet (it arrived during this run): the next run picks it up.
        log(f"{src}: not converted yet; left for the next run")
        return False
    wiki_events.emit("read", file=src)
    log(f"reading {src}")
    say("read", "reading the converted text")
    fm, body = split_frontmatter(read(mirror))
    method, note = fm_get(fm, "method"), fm_get(fm, "note")
    if method == "ERROR" or note == "scanned-no-ocr" or not body.strip():
        review("general", src, "could not be read", [
            f"{name}: conversion said {method or 'nothing'} {note}".strip()
            + ". It stays in the queue; check that it opens and is not a scan without OCR."])
        return False

    model.deadline = time.time() + DOC_BUDGET_SECONDS
    try:
        # 1. Every question first ...
        parts = chunks_of(body)
        truncated = len(parts) > MAX_PARTS
        parts = parts[:MAX_PARTS]
        results = []
        for i, part in enumerate(parts, 1):
            if len(parts) > 1:
                wiki_events.emit("progress", file=src, msg=f"Reading {name}, part {i} of {len(parts)}")
                say("ask", "pulling out what it says", n=i, of=len(parts))
            else:
                say("ask", "pulling out what it says")
            q = (f"Document file name: {name}\n"
                 + (f"This is part {i} of {len(parts)}.\n" if len(parts) > 1 else "")
                 + "Extract what it states.\n\n<document>\n" + part + "\n</document>")
            results.append(model.ask(sysmsg, q, EXTRACT_SCHEMA, max_tokens=2000))
        d = merge_parts(results)
        if len(results) > 1:
            say("ask", f"condensing the {len(results)} parts into one summary")
            points = "\n".join(f"- {p}" for p in d["summary"])
            c = model.ask(sysmsg, f"These are summary points from the parts of one document, {name}.\n"
                          "Write its title and a summary of at most 10 points.\n\n" + points,
                          CONDENSE_SCHEMA, max_tokens=1200)
            d["title"] = clean_line(c.get("title") or d["title"], 160)
            d["summary"] = [clean_line(x, 300) for x in c.get("summary") or [] if str(x).strip()] or d["summary"]

        proj_list = projects()
        project = "general"
        if len(proj_list) > 1:
            say("ask", "choosing its project folder")
            pick = model.ask(sysmsg, "Which project folder does this document belong to? Choose "
                             "'general' unless one clearly fits.\n\nProjects: " + ", ".join(proj_list)
                             + f"\n\nDocument: {d['title']}\n" + "\n".join(d["summary"][:6]),
                             obj({"project": S("string", enum=proj_list)}), max_tokens=60)
            project = pick.get("project") or "general"

        dest = choose_dest(f"raw/{project}", name, src)
        plan = plan_entities(model, wiki, sysmsg, d, dest, body)
    finally:
        model.deadline = None

    # 2. ... then the writes, which need no model and cannot half-fail on a slow answer.
    say("write", "writing its pages")
    write_document_pages(wiki, src, dest, project, d, plan, truncated, len(parts))
    say("file", f"filing it in {os.path.dirname(dest)}")
    place(src, dest)
    wiki_events.emit("filed", file=src, to=dest)
    log(f"filed {src} -> {dest}")
    log_line("intake", project, d["title"] or name)
    return True


def merge_parts(results):
    first = results[0] if results else {}
    m = {"title": clean_line(first.get("title") or "", 160), "doc_type": first.get("doc_type") or "other",
         "doc_date": first.get("doc_date") or "", "summary": [], "facts": [], "entities": [],
         "identifiers": [], "decisions": [], "open_questions": []}
    if m["doc_type"] not in DOC_TYPES:
        m["doc_type"] = "other"
    seen = {}
    for r in results:
        if not m["doc_date"] and r.get("doc_date"):
            m["doc_date"] = r["doc_date"]
        m["summary"] += [clean_line(s, 300) for s in r.get("summary", []) if str(s).strip()]
        for f in r.get("facts", []):
            if str(f.get("fact", "")).strip():
                m["facts"].append({"about": clean_line(f.get("about", ""), 120), "fact": clean_line(f["fact"], 300)})
        for e in r.get("entities", []):
            name = clean_line(e.get("name", ""), 120)
            kind = e.get("kind") if e.get("kind") in ("company", "person", "product", "other") else "other"
            if not norm_full(name):
                continue
            key = (kind, norm_full(name))
            if key in seen:
                role = clean_line(e.get("role", ""), 160)
                if role and role not in seen[key]["role"]:
                    seen[key]["role"] = clean_line(seen[key]["role"] + "; " + role, 200)
                continue
            seen[key] = {"name": name, "kind": kind, "role": clean_line(e.get("role", ""), 160)}
            m["entities"].append(seen[key])
        m["identifiers"] += [i for i in r.get("identifiers", []) if isinstance(i, dict)]
        m["decisions"] += [clean_line(x, 300) for x in r.get("decisions", []) if str(x).strip()]
        m["open_questions"] += [clean_line(x, 300) for x in r.get("open_questions", []) if str(x).strip()]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", m["doc_date"] or ""):
        m["doc_date"] = ""
    return m


# The label a document prints just before an identifier decides what it is; the model's
# own label is only a hint (a small model files an SST number as a registration number).
ID_LABELS = [
    ("sst_no", r"\b(sst|sales\s*(and|&)\s*services?\s*tax|service\s*tax)\b"),
    ("tin_no", r"\b(tin|tax\s*(identification|id|ref(erence)?|no|number|file))\b"),
    ("bank_account", r"\b(account|a/c|acct?\.?\s*no|bank)\b"),
    ("licence_no", r"\b(licen[cs]e|permit)\b"),
    ("registration_no", r"\b(company|co\.|roc|ssm|business|registration|reg\.|incorporat\w*)\b"),
]


def identifier_kind(value, body):
    """The kind of identifier `value` is, read from the label in front of it in the
    document, or None when the document does not contain it or does not say."""
    v = re.sub(r"\s+", "", value or "")
    if len(v) < 4 or sum(c.isdigit() for c in v) < 4:
        return None  # "SDN BHD" is not a number
    pattern = r"\s*".join(re.escape(c) for c in v)
    for m in re.finditer(pattern, body, re.I):
        window = body[max(0, m.start() - 48):m.start()]
        window = window.split("\n")[-1] if "\n" in window[-30:] else window
        for kind, rx in ID_LABELS:
            if re.search(rx, window, re.I):
                return kind
    return None


def plan_entities(model, wiki, sysmsg, d, dest, body):
    """For each company and person: its page (existing or new), verified identifiers, its
    own facts, and contradictions with the existing page. Asks the model; writes nothing."""
    plan = []
    people = [e for e in d["entities"] if e["kind"] in ("company", "person")]
    checks = [e["name"] for e in people if d["facts"] and wiki.find_entity(e["name"], e["kind"])]
    for e in people:
        rel = wiki.find_entity(e["name"], e["kind"])
        facts = [f for f in d["facts"] if f["about"] and norm_full(f["about"]) == norm_full(e["name"])]
        ids = {}
        for i in d["identifiers"]:
            if norm_full(i.get("entity", "")) != norm_full(e["name"]):
                continue
            kind = identifier_kind(i.get("value"), body)
            if kind:
                ids.setdefault(kind, clean_line(i["value"], 80))
        conflicts = []
        if rel and d["facts"]:
            say("check", f"checking {clean_line(e['name'], 60)} against its page",
                n=checks.index(e["name"]) + 1 if e["name"] in checks else None, of=len(checks) or None)
            existing = split_frontmatter(read(rel))[1][:6000]
            new = "\n".join(f"- {f['about'] + ': ' if f['about'] else ''}{f['fact']}" for f in d["facts"])
            try:
                c = model.ask(sysmsg, f"Existing wiki page about {e['name']}:\n<page>\n{existing}\n</page>\n\n"
                              f"New statements from {os.path.basename(dest)}:\n{new}\n\n"
                              "Compare them one by one. A contradiction is the page and a new statement "
                              "giving different values for the same thing: a different price, amount, "
                              "payment term, period, date, quantity, address, registration number, or "
                              "person in a role (for example, a price of RM 10 on the page and RM 12 in "
                              "the new statements). List every contradiction, quoting both sides. "
                              "Statements that only add new information are not contradictions; return "
                              "an empty list if there are none.",
                              CONFLICT_SCHEMA, max_tokens=700)
                for x in c.get("conflicts", []):
                    conflicts.append(f"{e['name']}: the page says \"{clean_line(x.get('existing'), 160)}\"; "
                                     f"{os.path.basename(dest)} says \"{clean_line(x.get('new'), 160)}\"")
            except (ModelUnavailable, DocTimeout):
                raise
            except Exception as err:  # a failed check never blocks filing; it is logged
                log(f"conflict check skipped for {rel}: {err}")
        plan.append({"e": e, "rel": rel, "facts": facts, "ids": ids, "conflicts": conflicts})
    return plan


def write_document_pages(wiki, src, dest, project, d, plan, truncated, n_parts):
    title = d["title"] or os.path.splitext(os.path.basename(src))[0]
    slug = slugify(os.path.splitext(os.path.basename(dest))[0])
    summary_rel = wiki.unique_path("sources", slug, source=dest)
    created = summary_rel not in wiki.pages

    # The summary page first, so every link to it resolves even if a later step fails.
    ent_rels = []
    for p in plan:
        if p["rel"]:
            ent_rels.append(p["rel"])
        else:
            p["rel"] = wiki.unique_path("companies", slugify(p["e"]["name"]))
            ent_rels.append(p["rel"])
            wiki.pages.setdefault(p["rel"], {"title": p["e"]["name"], "aliases": [], "tags": [p["e"]["kind"]]})
    conflicts = [c for p in plan for c in p["conflicts"]]

    lines = ["---", f"title: {yq(label(title, 160))}", "type: summary", f"tags: [{yq(d['doc_type'])}]",
             f"sources: [{yq(dest)}]",
             "status: needs-review" if (truncated or conflicts) else "status: draft",
             f"updated: {today()}", f"project: {yq(project)}"]
    if d["doc_date"]:
        lines.append(f"doc_date: {yq(d['doc_date'])}")
    lines += ["engine: local", "---", ""]
    lines.append(f"> Summary of {raw_link(dest)} written by the local model on this Mac. "
                 "Check figures against the original before relying on them.")
    lines.append("")
    if d["summary"]:
        lines += ["## Summary", ""] + [f"- {esc(s)}" for s in d["summary"]] + [""]
    if d["facts"]:
        lines += ["## Key facts", ""]
        lines += [f"- **{esc(f['about'], 120)}:** {esc(f['fact'])}" if f["about"] else f"- {esc(f['fact'])}"
                  for f in d["facts"]]
        lines.append("")
    if plan:
        lines += ["## Companies and people", ""]
        lines += [f"- {wiki.link(p['rel'], p['e']['name'])}" + (f" — {esc(p['e']['role'], 160)}" if p["e"]["role"] else "")
                  for p in plan]
        lines.append("")
    if d["decisions"]:
        lines += ["## Decisions", ""] + [f"- {esc(x)}" for x in d["decisions"]] + [""]
    if d["open_questions"]:
        lines += ["## Open questions", ""] + [f"- {esc(x)}" for x in d["open_questions"]] + [""]
    if truncated:
        lines += [f"_Only the first {n_parts} parts of this long document were read._", ""]
    write(summary_rel, "\n".join(lines))
    page_event(summary_rel, created, wiki)

    for p in plan:
        write_entity(wiki, p, d, dest, summary_rel, title)

    proj_rel = f"wiki/projects/{project}.md"
    if proj_rel not in wiki.pages or not os.path.exists(os.path.join(WIKI_DIR, proj_rel)):
        write(proj_rel, "\n".join(["---", f"title: {yq(project)}", "type: project", "tags: [project]",
                                   "sources: []", "status: draft", f"updated: {today()}", "---", "",
                                   f"Documents and decisions filed under `raw/{project}/`.", ""]))
        page_event(proj_rel, True, wiki)
    if not wiki.has_link(proj_rel, summary_rel):
        append_under(proj_rel, "Documents", [f"- {wiki.link(summary_rel, title)} — {d['doc_type']}"
                                             + (f", {d['doc_date']}" if d["doc_date"] else "")])
        touch_page(proj_rel)
        page_event(proj_rel, False, wiki)

    index_add(wiki, summary_rel, "Sources", (d["summary"] or [title])[0])
    for p in plan:
        index_add(wiki, p["rel"], "Companies and people", p["e"]["role"] or p["e"]["kind"])

    if conflicts:
        review(project, summary_rel, "a new document disagrees with an existing page", conflicts)
    if truncated:
        review(project, summary_rel, "long document only partly read",
               [f"{os.path.basename(dest)} has more than {MAX_PARTS} parts; only the first were read."])


def write_entity(wiki, p, d, dest, summary_rel, summary_title):
    e, rel = p["e"], p["rel"]
    bullet = f"- {today()} — {esc(e['role'] or 'mentioned', 160)} ({wiki.link(summary_rel, summary_title)})"
    facts = [f"- {esc(f['fact'])} ({wiki.link(summary_rel, 'source')})" for f in p["facts"]]
    path = os.path.join(WIKI_DIR, rel)
    if not os.path.exists(path):
        lines = ["---", f"title: {yq(label(e['name'], 120))}", "type: entity",
                 f"tags: [{yq(e['kind'])}]", f"sources: [{yq(dest)}]", "status: draft",
                 f"updated: {today()}"]
        if e["kind"] == "company":
            alias = clean_line(SUFFIX_ORIG_RE.sub("", e["name"]).strip(" ,."), 120)
            if alias and alias != e["name"]:
                lines.append(f"aliases: [{yq(label(alias, 120))}]")
        if p["ids"]:
            lines.append("facts:")
            lines += [f"  {k}: {yq(v)}" for k, v in p["ids"].items()]
        lines += ["---", "", f"{esc(e['name'], 120)} ({e['kind']}).", ""]
        if facts:
            lines += ["## Facts", ""] + facts + [""]
        lines += ["## From sources", "", bullet, ""]
        write(rel, "\n".join(lines))
        page_event(rel, True, wiki)
        return
    if wiki.has_link(rel, summary_rel):
        return  # this document was already recorded here (a retry)
    if facts:
        append_under(rel, "Facts", facts)
    append_under(rel, "From sources", [bullet])
    fm, body = split_frontmatter(read(rel))
    if p["ids"] and fm and not re.search(r"^facts:", fm, re.M):
        # Verified identifiers fill an empty Info card; an existing card is never changed.
        fm = fm.rstrip("\n") + "\nfacts:\n" + "".join(f"  {k}: {yq(v)}\n" for k, v in p["ids"].items())
        write(rel, "---\n" + fm.strip("\n") + "\n---\n" + body)
    # A page a new document contradicts is flagged too, not only the new summary.
    touch_page(rel, add_source=dest, status="needs-review" if p["conflicts"] else None)
    page_event(rel, False, wiki)


# ==================================================================== packets ====
FIELDS = ("Type", "Summary", "Affects pages", "Details", "Supersedes", "Open questions")
FIELD = re.compile(r"^(?:[-*]\s*)?\*{0,2}(" + "|".join(FIELDS) + r")\*{0,2}\s*:\s*\*{0,2}\s*(.*)$", re.I)
HEAD = re.compile(r"^#{1,3}\s*\[(\d{4}-\d{2}-\d{2})\]\s*update\s*\|\s*([^|]*)\|\s*(.+)$", re.I)


def parse_packets(text_):
    packets, cur, field = [], None, None
    canon = {f.lower(): f for f in FIELDS}
    for line in text_.splitlines():
        h = HEAD.match(line.strip())
        if h:
            cur = {"date": h.group(1), "project": slugify(h.group(2).strip() or "general"),
                   "title": h.group(3).strip(), "Type": "", "Summary": "", "Affects pages": [],
                   "Details": [], "Supersedes": "", "Open questions": ""}
            packets.append(cur)
            field = None
            continue
        if cur is None:
            continue
        f = FIELD.match(line.strip())
        if f:
            field = canon[f.group(1).lower()]
            value = f.group(2).strip().rstrip("*").strip()
            if field in ("Details", "Affects pages"):
                if value:
                    cur[field] += [value] if field == "Details" else [t for t in re.split(r"[,;]", value) if t.strip()]
            else:
                cur[field] = value
            continue
        item = re.sub(r"^[-*]\s*", "", line.strip())
        if not item:
            continue
        if field == "Details":
            cur["Details"].append(item)
        elif field == "Affects pages":
            cur["Affects pages"] += [t for t in re.split(r"[,;]", item) if t.strip()]
        elif field in ("Summary", "Supersedes", "Open questions"):
            cur[field] = (cur[field] + " " + item).strip()
    return packets


def note_as_packet(text_, name):
    """A Markdown note without the packet header: applied as one fact update."""
    lines = [l for l in text_.splitlines()]
    title = next((re.sub(r"^#+\s*", "", l).strip() for l in lines if l.startswith("#")), "") \
        or os.path.splitext(name)[0]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text_) if p.strip() and not p.strip().startswith("#")]
    return {"date": today(), "project": "general", "title": clean_line(title, 120), "Type": "fact",
            "Summary": clean_line(paras[0] if paras else "", 600), "Affects pages": [],
            "Details": [clean_line(p, 600) for p in paras[1:20]], "Supersedes": "", "Open questions": ""}


def is_empty(v):
    return str(v).strip().lower().strip(".") in ("", "none", "n/a", "na", "-", "nil", "no")


def process_packet(model, wiki, src, sysmsg):
    Run.current = src
    wiki_events.emit("read", file=src)
    log(f"applying {src}")
    say("apply", "reading the update")
    text_ = read(src)
    if "<<<<<<<" in text_ and ">>>>>>>" in text_:
        text_ = re.sub(r"^(<<<<<<<|=======|>>>>>>>).*$", "", text_, flags=re.M)
    packets = parse_packets(text_) or ([note_as_packet(text_, os.path.basename(src))] if text_.strip() else [])
    archived = choose_dest("archive/inbox", os.path.basename(src), src)
    model.deadline = time.time() + DOC_BUDGET_SECONDS
    try:
        planned = [(p, plan_packet(model, wiki, p, sysmsg)) for p in packets]
    finally:
        model.deadline = None
    say("write", "writing the update into the wiki")
    for p, (targets, unresolved) in planned:
        apply_packet(wiki, p, archived, targets, unresolved)
    say("file", "archiving it")
    place(src, archived)
    wiki_events.emit("applied", file=src, to=archived)
    return True


def plan_packet(model, wiki, p, sysmsg):
    """The existing pages an update names: matched by code, and by the model only for the
    names code cannot match, choosing from candidates it is shown."""
    targets, unresolved = [], []
    for t in p["Affects pages"]:
        r = wiki.resolve(t)
        (targets.append(r) if r else unresolved.append(clean_line(t, 120)))
    if unresolved:
        words = set(norm_full(" ".join(unresolved) + " " + p["title"]).split())
        cands = sorted((x for x in wiki.pages if not x.startswith(("wiki/sources/", "wiki/updates/"))),
                       key=lambda x: (-len(words & set(norm_full(x.replace("/", " ").replace("-", " ")
                                                                  + " " + wiki.pages[x]["title"]).split())), x))
        cands = cands[:MAX_PAGE_CHOICES]
        if cands:
            say("ask", "matching the pages it names")
            listing = "\n".join(f"- {c} ({clean_line(wiki.pages[c]['title'], 80)})" for c in cands)
            try:
                ans = model.ask(sysmsg, "An update says it affects these pages: " + "; ".join(unresolved)
                                + f"\nUpdate: {p['title']}. {clean_line(p['Summary'], 400)}\n\n"
                                "Existing pages:\n" + listing + "\n\nWhich of the existing pages are meant? "
                                "Choose only clear matches; return an empty list if none fits.",
                                obj({"pages": S("array", items=S("string", enum=cands), maxItems=8)}),
                                max_tokens=400)
                targets += ans.get("pages", [])
            except (ModelUnavailable, DocTimeout):
                raise
            except Exception as err:
                log(f"page matching skipped: {err}")
    targets = [t for i, t in enumerate(targets) if t in wiki.pages and t not in targets[:i]]
    return targets, unresolved


def apply_packet(wiki, p, archived, targets, unresolved):
    kind = (str(p["Type"]).split("|")[0].strip().lower() or "fact")
    folder = "decisions" if kind == "decision" else "updates"
    slug = f"{p['date']}-{slugify(p['title'], 50)}"
    rel = wiki.unique_path(folder, slug, source=archived)
    created = rel not in wiki.pages
    targets = [t for t in targets if t != rel]

    lines = ["---", f"title: {yq(label(p['title'], 160))}", f"type: {'decision' if folder == 'decisions' else 'summary'}",
             f"tags: [{yq(slugify(kind, 30))}]", f"sources: [{yq(archived)}]",
             "status: needs-review" if not is_empty(p["Supersedes"]) else "status: active",
             f"updated: {today()}", f"project: {yq(p['project'])}", "engine: local", "---", ""]
    if p["Summary"]:
        lines += [owner_text(p["Summary"]), ""]
    if p["Details"]:
        lines += ["## Details", ""] + [f"- {owner_text(x)}" for x in p["Details"]] + [""]
    if targets:
        lines += ["## Affects", ""] + [f"- {wiki.link(t)}" for t in targets] + [""]
    if not is_empty(p["Supersedes"]):
        lines += ["## Supersedes", "", owner_text(p["Supersedes"]), ""]
    if not is_empty(p["Open questions"]):
        lines += ["## Open questions", "", owner_text(p["Open questions"]), ""]
    lines += [f"_From the Update Packet {raw_link(archived)}, dated {p['date']}._", ""]
    write(rel, "\n".join(lines))
    page_event(rel, created, wiki)

    entry = f"- **{p['date']}** — {esc(p['Summary'] or p['title'], 300)} ({wiki.link(rel, 'details')})"
    for t in targets:
        if wiki.has_link(t, rel):
            continue
        append_under(t, "Updates", [entry])
        touch_page(t, add_source=archived)
        page_event(t, False, wiki)

    index_add(wiki, rel, "Decisions" if folder == "decisions" else "Updates", p["Summary"] or p["title"])
    log_line("ingest", p["project"], p["title"])
    if not is_empty(p["Supersedes"]):
        review(p["project"], rel, "an update supersedes an earlier claim", [
            f"{p['title']}: supersedes \"{clean_line(p['Supersedes'], 240)}\".",
            "Find the old claim on the affected pages and mark it superseded."])
    if not is_empty(p["Open questions"]):
        review(p["project"], rel, "open questions from an update", [clean_line(p["Open questions"], 400)])
    if unresolved and not targets:
        review(p["project"], rel, "affected pages not found",
               [f"{p['title']} names pages that do not exist yet: {', '.join(unresolved)}."])


# ======================================================================= run =====
def pending(folder):
    out = []
    root = os.path.join(WIKI_DIR, folder)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fn in sorted(files):
            if not wiki_events.ignored_name(fn):
                out.append(os.path.relpath(os.path.join(dirpath, fn), WIKI_DIR).replace(os.sep, "/"))
    return out


def house_rules():
    try:
        return read("HOUSE-RULES.md")[:HOUSE_RULES_CHARS]
    except OSError:
        return ""


def load_config():
    try:
        with open(os.path.join(WIKI_DIR, "wiki.config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def run(server=None, only_docs=None, packets_only=False):
    docs = [] if packets_only else pending("raw/_intake")
    if only_docs is not None:
        docs = [d for d in docs if d in only_docs]
    packets = [] if only_docs is not None else [p for p in pending("raw/inbox") if p.endswith((".md", ".markdown"))]
    if not docs and not packets:
        return 0
    cfg = load_config().get("localModel") or {}
    sysmsg = system_prompt(house_rules())
    wiki_events.emit("claude-start", label="local")
    t0 = time.time()
    ok = True
    model = Model(url=server, cfg=cfg)
    try:
        wiki = Wiki()
        for kind, items, fn in (("document", docs, process_document), ("packet", packets, process_packet)):
            for src in items:
                try:
                    fn(model, wiki, src, sysmsg)
                except ModelUnavailable:
                    raise
                except Exception as e:  # one bad file never stops the others
                    ok = False
                    log(f"{src}: {e}")
                    wiki_events.emit("error", file=src, msg=f"{os.path.basename(src)}: {e}")
    except ModelUnavailable as e:
        log(str(e))
        wiki_events.emit("claude-end", label="local", ok=False, detail=str(e))
        return 2
    finally:
        model.stop()
    stats = model.stats
    secs = round(time.time() - t0)
    wiki_events.emit("claude-end", label="local", ok=True, turns=stats["calls"], cost=0,
                     detail=f"{secs}s, {stats['prompt_tokens']} tokens read, "
                            f"{stats['completion_tokens']} written")
    log(f"done in {secs}s: {stats}")
    return 0 if ok else 1


def check(server=None):
    cfg = load_config().get("localModel") or {}
    model = Model(url=server, cfg=cfg)
    try:
        a = model.ask("Answer briefly.", "Reply with the word ready.", obj({"answer": text(20)}), max_tokens=20)
    except (ModelUnavailable, RuntimeError, DocTimeout) as e:
        print(f"local model check failed: {e}", file=sys.stderr)
        return 2
    finally:
        model.stop()
    print(f"local model answered: {a.get('answer')!r}")
    return 0


def main(argv=None):
    # The runner's watchdog stops a run with SIGTERM: unwind normally, so the model
    # server is stopped too instead of being left holding its memory.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    ap = argparse.ArgumentParser(description="Build the wiki with a local model.")
    ap.add_argument("command", choices=["run", "check"])
    ap.add_argument("--server", help="use this llama-server URL instead of starting one")
    ap.add_argument("--docs", help="run: only the documents listed in this file, no packets")
    ap.add_argument("--packets", action="store_true", help="run: only the Update Packets")
    a = ap.parse_args(argv)
    wiki_netguard.install()
    os.chdir(WIKI_DIR)
    server = a.server or os.environ.get("WIKI_LOCAL_SERVER_URL")  # tests
    if a.command == "check":
        return check(server)
    only_docs = None
    if a.docs:
        with open(a.docs, encoding="utf-8") as f:
            only_docs = {line.strip() for line in f if line.strip()}
    return run(server, only_docs=only_docs, packets_only=a.packets)


if __name__ == "__main__":
    sys.exit(main())
