#!/usr/bin/env python3
"""local_engine.py — build the wiki with a model that runs on this Mac.

Used instead of Claude when wiki.config.json says "engine": "local". Nothing leaves the
Mac: the model runs in llama.cpp's llama-server, started for the run on 127.0.0.1 and
stopped afterwards, so its memory is free whenever the wiki is idle.

A small local model is good at answering one narrow question about one text; it is not
reliable at editing many files on its own. So this script does the steps, the model only
answers, in JSON forced to match a schema, and code checks every answer against the
document before anything is written:

  documents (raw/_intake/, already converted to cache/md/ by the runner)
    1. read the Markdown mirror in parts that fit the model's context;
    2. per part, focused questions that share the document as a prompt prefix (the model
       reads it once): what it is (title, type, date, summary, decisions, obligations,
       open questions); who it names (each company, person, product and place, with its role);
       every figure (amounts, prices, fees, dates, durations, rates, addresses), each
       labelled with what it is and whom it is about; identifiers; then one more question
       about the figures and names that patterns find in the text but the answers missed;
    3. per document: the relationships it states between the parties, the topic pages it
       informs (finance and legal, how the business runs) and its project folder;
    4. grounding: a figure, identifier, summary point or decision is kept only if the
       document contains its numbers and dates (scripts/local_facts.py);
    5. figures about a company, person or product go into archive/claims.jsonl; a standing
       term (payment terms, a price, a fee, a notice period, an address) that differs from
       what an earlier source said is a contradiction: both values and sources go to
       wiki/_review.md and the page shows the new value with the old one;
    6. only then write: the document's summary page (wiki/sources/), the pages of the
       companies and people (wiki/companies/) and products (wiki/products/), topic pages
       (wiki/finance-legal/, wiki/how-it-runs/), decision pages (wiki/decisions/), the
       project page; then file the original under raw/<project>/, unrenamed unless the
       name is taken.
  Update Packets (raw/inbox/)
    parsed by code; one page per packet (wiki/decisions/ or wiki/updates/), a dated entry
    on every affected page (the model only maps page names it cannot match, and reads the
    packet's figures into the claims store), a topic page for an affected page that does
    not exist yet in a known section, review entries for open questions; the packet moves
    to archive/inbox/. A plain note without the packet header is applied as one.
  at the end of the run
    the model writes a short grounded overview for every page the run touched; code
    builds the rest of each page's engine block (Current facts, Related, Documents) and the
    overview's "At a glance".

Pages are changed only inside the engine's own block (scripts/local_pages.py), which a
person's edit locks; everything else on an existing page is only ever appended to.
Frontmatter, wikilinks, index.md and log.md are written by code, and every string that
came from a document or the model is escaped, so neither a model mistake nor a hostile
document can break the site or put markup in it.

Usage:
  local_engine.py run [--server URL]   process everything waiting
  local_engine.py run --docs FILE      only the documents listed in FILE (one batch; the
                                       runner calls this), no Update Packets
  local_engine.py run --packets        only the Update Packets
  local_engine.py check                start the model and ask one question (installer)
  run --packet-list FILE  with --packets: only the packets listed in FILE (those that had
                        finished arriving when the run began; the runner calls this)
  run --tried FILE      at the end of the reading, write the documents it tried (all it was
                        given but those a spending cap left unread, which must not count a try)
  run --capped FILE     at the end of the reading, write the documents a spending cap left
                        unread (never put to Claude); the runner counts a try for every
                        other document of the batch, so one this script never saw is set
                        aside in the end instead of being read again and again
  run --inflight FILE   keep the files being read right now in FILE, so that if this process
                        dies (a native crash) the runner knows which ones to count a try for
  run --alone FILE      with Claude reading: read the documents listed in FILE first, one at
                        a time (they were being read when it died the run before)
  run --why FILE        when it exits 2, the reason (a sign-in, a usage limit, ...) in FILE
  Lists (--docs, --alone, --packet-list) hold one path per line, exactly: a name may end
  in a space. Folders whose name starts with a dot are skipped, as the runner skips them.

Exit status: 0 all done; 1 some files failed (they stay queued and are retried, then set
aside, like any file); 2 the model could not run at all (nothing is counted against the
files).

Standard library only. Progress goes to the activity log (scripts/wiki_events.py), and
each step (loading the model, each question, writing, filing) to its live line, with the
model's answer streamed so the line can count the words as they arrive.
"""
import argparse
import concurrent.futures
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
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WIKI_DIR, "scripts"))
import local_facts as F  # noqa: E402
import local_pages as LP  # noqa: E402
import wiki_claude  # noqa: E402
import wiki_review  # noqa: E402
import wiki_events  # noqa: E402
import wiki_menu  # noqa: E402
import wiki_netguard  # noqa: E402
import wiki_settings  # noqa: E402
from local_pages import (SUFFIX_ORIG_RE, SUFFIX_RE, SUFFIX_WORDS, _flow_items, _key_block,  # noqa: E402,F401
                         clean_line, esc, fm_get, fm_list, fm_set, label, norm_full, owner_text,
                         raw_link, same_entity, split_frontmatter, split_suffix, yq)

CHUNK_CHARS = 18000          # about 5-6k tokens: leaves room for instructions and the answer
MAX_PARTS = 24               # longer documents are summarised from their first parts
DOC_BUDGET_SECONDS = 2700    # one document may take this long before it counts as failed
PROSE_BUDGET_SECONDS = 600   # one page's overview, at the end of a run
TOKEN_NOTE_SECONDS = 1.5     # how often the live line hears how far an answer has got
HOUSE_RULES_CHARS = 2500
MAX_PAGE_CHOICES = 150
IGNORED_DIRS = {"_intake", "inbox", "_needs-review", "_unfiled"}
DOC_TYPES = ["contract", "agreement", "invoice", "quotation", "purchase-order", "receipt",
             "statement", "report", "minutes", "letter", "email", "policy", "procedure",
             "form", "presentation", "spreadsheet", "brochure", "certificate", "other"]
ID_KINDS = ["registered_name", "registration_no", "tin_no", "sst_no", "licence_no",
            "bank_account", "id_no", "epf_no", "socso_no"]
PERSON_IDS = {"id_no", "epf_no", "socso_no"}   # a person's identity card or passport, EPF, SOCSO


class ModelUnavailable(Exception):
    """The model could not be started or stopped answering: the run stops, and no file
    is counted as failed."""


class DocTimeout(Exception):
    """One document took longer than its budget: it counts as a failed attempt."""


def today():
    return datetime.date.today().isoformat()


def log(msg):
    wiki_events.log(f"local: {msg}")


def slugify(text, limit=60):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:limit].rstrip("-")
    if not t:  # a name in another script: keep it stable and unique
        t = "page-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return t


def share_a_row(a, b, text):
    """In a table each row is one record. Two parties are linked from a table only by a row
    that names both, when either is named only in rows: a ledger's catering line for one
    customer says nothing of the supplier on the line above. True when it cannot tell."""
    lines = [(l.lstrip().startswith("|"), f" {norm_full(l)} ") for l in (text or "").splitlines()]
    ka, kb = (f" {split_suffix(x)[0]} " for x in (a, b))
    ha, hb = [t for t, l in lines if ka in l], [t for t, l in lines if kb in l]
    if not ka.strip() or not kb.strip() or not ha or not hb or not (all(ha) or all(hb)):
        return True
    return any(t and ka in l and kb in l for t, l in lines)


# ================================================================ frontmatter ====
# Reading frontmatter (split_frontmatter, fm_list, fm_get, fm_set) is in local_pages.py,
# shared with scripts/wiki_menu.py.
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
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, full)
    except BaseException:   # a full disk or a signal leaves no half-written copy behind
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def append_under(path, heading, lines):
    """Append bullet lines under a '## heading' section (created at the end if missing).
    The engine's own block is never written into: a heading inside it does not count, and
    a section ends where the block starts."""
    text = read(path).rstrip("\n") + "\n"
    block = "".join(l + "\n" for l in lines)
    found = LP.find_block(text)
    span = (found[0], found[1]) if found else (-1, -1)
    m = next((x for x in re.finditer(rf"^## {re.escape(heading)}\s*$", text, re.M)
              if not span[0] <= x.start() < span[1]), None)
    if not m:
        text += f"\n## {heading}\n\n" + block
    else:
        nxt = re.search(r"^## |^<!-- wiki-engine:start", text[m.end():], re.M)
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
def supports(binary, flag):
    """Whether this llama-server knows a command-line flag (older builds refuse unknown ones)."""
    try:
        r = subprocess.run([binary, "--help"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        return flag in (r.stdout + r.stderr)
    except (OSError, subprocess.TimeoutExpired):
        return False


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
        if supports(binary, "--cache-ram"):
            # Newer servers also keep past prompts in RAM, up to 8 GB by default: on a 16 GB
            # Mac that is memory the model needs. The questions about one document reuse
            # it from the model's working memory anyway, so this extra cache is off.
            args += ["--cache-ram", str(int(self.cfg.get("cacheRamMiB", 0)))]
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

    def ask(self, system, user, schema, max_tokens=1500, kind=""):
        """One question, answered as JSON that matches schema. Returns the parsed object.
        kind names the call for Claude's summary and means nothing here."""
        if not self.url:
            self.start()
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "answer", "strict": True, "schema": schema}},
            "chat_template_kwargs": {"enable_thinking": False},
            "cache_prompt": True,  # questions about one document share it as their prefix
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
            if e.code == 503:  # still loading, or overloaded: not this file's fault
                raise ModelUnavailable(f"the model is not ready ({e.read()[:200]!r})")
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


KINDS = ["company", "person", "product", "place", "other"]
# The topic pages a small business usually keeps: (section, title, its place in the wiki's
# menu). A document adds to these (or to topic pages the wiki already has); a new topic is
# proposed only when none fits, so the same subject never ends up spread over pages named
# after single documents. The list is kept in scripts/wiki_menu.py, whose rules place
# these pages by their titles.
TOPICS = wiki_menu.STARTER_TOPICS
DECISION_TYPES = {"minutes", "letter", "email", "policy", "procedure", "report", "presentation", "other"}
DECISION_CUE = re.compile(r"\b(resolved|resolution|decided|decision|approved|agreed to|agreed that|will proceed)\b", re.I)
SEED_PAGES = {"index", "how-this-wiki-works", "raw-to-markdown-conversion"}
THIS_DOC = "this document"
SECTIONS = {"finance-legal": "Finance and legal", "how-it-runs": "How it runs"}

OVERVIEW_SCHEMA = obj({
    "title": text(160),
    "doc_type": S("string", enum=DOC_TYPES),
    "doc_date": text(10),
    "summary": S("array", items=text(300), maxItems=8),
    "decisions": S("array", maxItems=5, items=obj({"title": text(90), "decision": text(300), "date": text(10)})),
    "obligations": S("array", items=text(240), maxItems=8),
    "open_questions": S("array", items=text(300), maxItems=5),
})
PARTIES_SCHEMA = obj({"parties": S("array", maxItems=25, items=obj({
    "name": text(120), "kind": S("string", enum=KINDS), "role": text(160)}))})


def figures_schema(names, n=30):
    return obj({"figures": S("array", maxItems=n, items=obj({
        "about": S("string", enum=names + [THIS_DOC]), "attribute": S("string", enum=F.ATTRIBUTES),
        "qualifier": text(100), "value": text(120)}))})


def ids_schema(names):
    return obj({"identifiers": S("array", maxItems=10, items=obj({
        "entity": S("string", enum=names), "kind": S("string", enum=ID_KINDS), "value": text(80)}))})


def second_schema(names, figures, people):
    props = {}
    if figures:
        props["figures"] = S("array", maxItems=len(figures), items=obj({
            "item": S("string", enum=figures), "about": S("string", enum=names + [THIS_DOC]),
            "attribute": S("string", enum=F.ATTRIBUTES), "qualifier": text(100)}))
    if people:
        props["parties"] = S("array", maxItems=len(people), items=obj({
            "name": S("string", enum=people), "kind": S("string", enum=KINDS), "role": text(160)}))
    return obj(props)


def relations_schema(names):
    return obj({"relations": S("array", maxItems=15, items=obj({
        "subject": S("string", enum=names), "relation": S("string", enum=LP.RELATIONS),
        "object": S("string", enum=names)}))})


def topics_schema(choices, proj_list):
    props = {"topics": S("array", maxItems=3, items=obj({
        "page": S("string", enum=choices + ["new"]), "section": S("string", enum=list(SECTIONS)),
        "title": text(80), "points": S("array", items=text(240), maxItems=4)}))}
    if len(proj_list) > 1:
        props["project"] = S("string", enum=proj_list)
    props.update(menu_field())
    return obj(props)


CONDENSE_SCHEMA = obj({"title": text(160), "summary": S("array", items=text(300), maxItems=10)})

CONFLICT_SCHEMA = obj({"conflicts": S("array", maxItems=5, items=obj({
    "existing": text(240), "new": text(240), "explanation": text(240)}))})

PROSE_SCHEMA = obj({"sentences": S("array", items=text(300), maxItems=4)})

Q_OVERVIEW = (
    "Answer about this document: its title (as a reader would name it, with its number if it has "
    "one), its type, its date (YYYY-MM-DD; empty if it has none), up to 8 summary points that "
    "carry the key names, amounts and dates, the decisions it records (each with a short title, "
    "the decision in one sentence, and its date), the obligations it sets (who must do what, by "
    "when), and the questions it leaves open.")
Q_PARTIES = (
    "List every company, organisation, person and product or service this document names, and "
    "each place that matters to the business (a site, an outlet, an office). For each: the name "
    "exactly as written (a company's full legal name when the document gives it; a person's name "
    "without Mr or Ms), what kind it is, and its role in this document, for example supplier, "
    "buyer, customer, landlord, tax agent, director, signatory, finance manager, contact, product "
    "bought.")
Q_FIGURES = (
    "List every figure this document states: amounts, prices, fees, quantities, percentages, "
    "dates, deadlines, durations and periods, rates, and addresses, and a person's salary, date of "
    "birth, phone number and email. One entry per figure, including table rows. For each:\n"
    "- about: whom or what the figure describes, from this list: {names}; or \"this document\" "
    "for the document's own numbers, totals and dates;\n"
    "- attribute: what kind of figure it is. payment_terms is only the time a buyer has to pay; "
    "a notice to end or change something is notice_period; how long an agreement runs is "
    "contract_term; a charge for a service is fee; a person's pay is salary;\n"
    "- qualifier: a few words saying exactly what it is for, e.g. \"washed arabica coffee beans\", "
    "\"annual tax compliance\", \"price revision notice\", \"Q1 2026\";\n"
    "- value: copied exactly as written, with its currency or unit.")
# For short texts (a page a person wrote, an Update Packet): the full list above can get an
# empty answer from a small model on a few lines; this plain question does not.
Q_SHORT = ("What payment terms, prices, fees, quantities, periods, dates, rates or addresses does this text "
           "give{about}? List each one, with what it is for.")
Q_IDS = (
    "List the identifiers this document gives for {names}: registered names, company or "
    "registration numbers, tax numbers (TIN, SST, GST), licence numbers, bank account numbers, and a "
    "person's identity card (IC, NRIC, MyKad) or passport number and EPF (KWSP) and SOCSO (PERKESO) "
    "numbers. Copy each value exactly as written. Leave the list empty if there are none.")


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
                           "tags": [t.lower() for t in (fm_list(fm, "tags") or [])],
                           "engine": fm_get(fm, "engine") == "local"}

    def reserve(self, rel, title, kind, aliases=()):
        """A page this document will create: known now, so links to it get its title."""
        self.pages.setdefault(rel, {"title": title, "aliases": list(aliases), "tags": [kind], "engine": True})

    def find_product(self, name):
        """The product's page: the same name, or a name whose words are all in the other's
        ("coffee beans" and "washed arabica coffee beans") when only one page fits."""
        prods = [(rel, [p["title"]] + p["aliases"]) for rel, p in self.pages.items() if rel.startswith("wiki/products/")]
        hits = [rel for rel, names in prods if any(norm_full(n) == norm_full(name) for n in names)]
        if hits:
            return hits[0]
        def words(n):  # "coffee bean deliveries" and "coffee beans" are the same product
            return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in norm_full(PRODUCT_NOISE.sub(" ", n)).split()}
        mine = words(name)
        close = [rel for rel, names in prods
                 if len(mine) >= 2 and any(words(n) <= mine or mine <= words(n) for n in names)]
        return close[0] if len(close) == 1 else None

    def topics(self):
        """Topic pages a document can add to: finance-legal/ and how-it-runs/, without the
        folder index and the pages that explain the wiki itself."""
        return [(rel, p["title"]) for rel, p in sorted(self.pages.items())
                if rel.split("/")[1] in SECTIONS and os.path.basename(rel)[:-3] not in SEED_PAGES]

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
            # The short alias the engine derives ("Northwind Trading") must not let
            # "Northwind Trading Pte Ltd" match the "... Sdn Bhd" page: only the title and
            # aliases that carry a legal suffix are compared by their core name.
            elif kind == "company" and any(same_entity(n, name) for n in names
                                           if n == p["title"] or split_suffix(n)[1]):
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
        """wiki/<folder>/<slug>.md, or -2, -3... when a different source already owns it,
        or a page this run has planned but not written yet."""
        n = 1
        while True:
            rel = f"wiki/{folder}/{slug}{'' if n == 1 else '-' + str(n)}.md"
            if rel not in self.pages:
                return rel
            if source and os.path.exists(os.path.join(WIKI_DIR, rel)):
                srcs = fm_list(split_frontmatter(read(rel))[0], "sources") or []
                if source in srcs:
                    return rel  # the same document again (a retry): rewrite its own page
                if any(os.path.basename(x) == os.path.basename(source) and x.startswith("raw/")
                       and not os.path.exists(os.path.join(WIKI_DIR, x)) for x in srcs):
                    return rel  # an earlier attempt that never got as far as filing it
            n += 1


def projects():
    out = []
    root = os.path.join(WIKI_DIR, "raw")
    for d in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if os.path.isdir(os.path.join(root, d)) and d not in IGNORED_DIRS and not d.startswith((".", "_")):
            out.append(d)
    if "general" not in out:
        out.append("general")
    return out


class Run:
    current = ""      # the file being worked on, for events
    company = ""      # the business's own company (wiki.config.json "company")
    reader = "local"  # "claude": the fast pipeline, Claude reads (scripts/wiki_claude.py)
    lanes = 1         # documents read at once
    cap = 0.0         # the fast pipeline's spending cap for one batch (maxSpendPerBatchUsd)
    doc_budget = DOC_BUDGET_SECONDS
    prose_budget = PROSE_BUDGET_SECONDS
    capped = set()    # documents never put to Claude because the batch reached its cap
    alone = set()     # documents to read first, one at a time (--alone)
    inflight = set()  # files being read right now ...
    inflight_path = ""  # ... kept in this file for the runner (--inflight)
    inflight_lock = threading.Lock()
    why_path = ""     # why the model or Claude could not run, for the runner (--why)
    models = {}       # Claude's model per kind of call, where it is not the reader's ("fast")
    efforts = {}      # Claude's effort per kind of call ("fast"); none: Claude's own default
    instructions_in_system = False   # the reading instructions in the system prompt ("fast")
    overviews = {}    # when an overview is not written again (wiki.config.json "overviews")
    overview_counts = {}  # this run's overviews: written, skipped (and why), failed
    routing = {}      # which reader each document goes to ("fast": "routing"); empty: one reader
    routed = {}       # this run's routing: how many documents went where, and why
    routed_lock = threading.Lock()


def in_flight(src, on):
    """Note that a file is being read (on) or done with (off). The runner reads the list if
    this process dies, to count a try for those files only."""
    if not Run.inflight_path:
        return
    with Run.inflight_lock:
        (Run.inflight.add if on else Run.inflight.discard)(src)
        tmp = f"{Run.inflight_path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("".join(p + "\n" for p in sorted(Run.inflight)))
            os.replace(tmp, Run.inflight_path)
        except OSError:
            pass


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
    # A review item's id is ours, not a document's: it stays code, as the runner writes it.
    text_ += "".join(f"- {l}\n" if re.fullmatch(r"Review item: `RV-[A-Za-z0-9-]+`", l) else f"- {esc(l, 600)}\n"
                     for l in lines)
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


def doc_prefix(name, part, i, n):
    """The start of every question about one part: the same text each time, so the model
    server reuses what it already read (its prompt cache) and only reads the question."""
    return (f"Document file name: {name}\n" + (f"This is part {i} of {n}.\n" if n > 1 else "")
            + "<document>\n" + part + "\n</document>\n\n")


def ask_safe(model, sysmsg, user, schema, max_tokens, what):
    """A question whose failure (an answer in the wrong shape) costs only its own answer."""
    try:
        return model.ask(sysmsg, user, schema, max_tokens=max_tokens)
    except (ModelUnavailable, DocTimeout):
        raise
    except Exception as e:
        log(f"{what} skipped: {e}")
        return {}


PART_PAYMENT = re.compile(r"\b(instal+ments?|partial|part[- ]payment|deposit paid|advance|down ?payment|balance due|"
                          r"first|second|third|final) (instal+ment|payment|part)\b|\binstal+ment\b", re.I)
UPPER_KEEP = {"Plc": "PLC", "Llc": "LLC", "Llp": "LLP", "Gmbh": "GmbH", "Pte": "Pte", "Ii": "II", "Iii": "III"}
LEGAL_END = re.compile(rf"(?:^|\s)(?i:{F.COMPANY_SUFFIX})\s*$")


def nice_title(title):
    """'SST-02 RETURN (SERVICE TAX) — SUMMARY' (a heading) -> 'SST-02 return (service tax) — summary'."""
    letters = [c for c in title if c.isalpha()]
    if len(letters) < 8 or sum(c.isupper() for c in letters) < 0.8 * len(letters):
        return title
    words = [w if re.search(r"\d", w) else w.lower() for w in title.split(" ")]
    out = " ".join(words)
    return out[:1].upper() + out[1:]


def nice_name(name):
    """'NORTHWIND TRADING SDN BHD' (a letterhead) -> 'Northwind Trading Sdn Bhd'."""
    if not name.isupper() or len(name) <= 4:
        return name
    words = [w.capitalize() if w.isalpha() else w for w in name.split(" ")]
    return " ".join(UPPER_KEEP.get(w, w) for w in words)


def clean_name(name):
    return nice_name(clean_line(F.HONORIFIC_RE.sub("", clean_line(name, 120)), 120).strip(" ,.;:"))


PRODUCT_NOISE = re.compile(r"\b(deliver(?:y|ies)|orders?|shipments?|purchases?|consignments?|batch(?:es)?)\b", re.I)
# A person's name, as a reader's "person" must look to get a page: two to eight words, each
# capitalised ("Siew-Ling", "O'Brien", "McDonald"), a surname in capitals ("TAN Ah Kow"),
# an initial, or a link ("Muthu A/L Raman", "Siti binti Ali", "Nurul Aina bt. Rahman",
# "Mohd Rizal B Ahmad", "Abdullah @ Ah Kow"), with a given name after a comma ("Chong Wei
# Liang, Jason"). "Directors", "Shift leads": a group.
_NAME_LINK = r"(?i:bin|binti|bte?\.?|bt\.?|b\.?|a/l|a/p|s/o|d/o)|van|de|@"
_NAME_WORD = (rf"(?:[A-Z](?:[a-z]{{1,2}}[A-Z]|['’][A-Z])?[a-z'’.]+(?:-[A-Za-z][a-z'’.]*)*|[A-Z]{{2,}}|[A-Z]\.|"
              rf"{_NAME_LINK})")
PERSON_NAME = re.compile(rf"{_NAME_WORD}(?:(?:\s+|,\s*){_NAME_WORD}){{1,7}}")


def clean_parties(items):
    out = []
    for e in items or []:
        if not isinstance(e, dict):
            continue
        name = clean_name(e.get("name"))
        kind = e.get("kind") if e.get("kind") in KINDS else "other"
        if kind in ("person", "other") and LEGAL_END.search(name):
            kind = "company"  # "... Sdn Bhd" is a company whatever the answer says
        if kind == "other" and wiki_menu.government_page(name):
            kind = "company"  # a government body the menu lists has a page, like a company
        if kind == "person" and not PERSON_NAME.fullmatch(name):
            kind = "other"  # "Directors", "Shift leads": a group, not a person with a page
        if kind == "product" and re.search(r"\d", name) and not re.search(r"[a-z]{3,}", name):
            kind = "other"  # "SST-02": a form, not a product
        if norm_full(name):
            out.append({"name": name, "kind": kind, "role": clean_line(e.get("role"), 160)})
    return out


def merge_parties(parties):
    """One entry per party: the same name twice, or a company with and without its legal
    suffix, is one party; its roles are joined and the fuller name kept."""
    out = []
    for p in parties:
        for q in out:
            same = norm_full(q["name"]) == norm_full(p["name"])
            if same or ("company" in (p["kind"], q["kind"]) and same_entity(q["name"], p["name"])):
                if q["kind"] != p["kind"] and "company" in (p["kind"], q["kind"]):
                    q["kind"] = "company"
                elif q["kind"] != p["kind"] and not same:
                    continue
                if len(p["name"]) > len(q["name"]) or (q["name"].isupper() and not p["name"].isupper()
                                                       and len(p["name"]) >= len(q["name"])):
                    q["name"] = p["name"]
                if p["role"] and norm_full(p["role"]) not in norm_full(q["role"]):
                    q["role"] = clean_line(f"{q['role']}; {p['role']}" if q["role"] else p["role"], 200)
                q["variants"] = list(dict.fromkeys(q.get("variants", []) + [p["name"]] + p.get("variants", [])))
                break
        else:
            out.append(dict(p, variants=list(dict.fromkeys([p["name"]] + p.get("variants", [])))))
    return out


def named(parties):
    """Parties that can have a page (and be what a figure is about)."""
    return [p["name"] for p in parties if p["kind"] in ("company", "person", "product")]


def mentioned(name, names):
    """Whether a name found by pattern is among the model's names (either way round)."""
    n = norm_full(name)
    return any(n == norm_full(x) or same_entity(name, x) or (len(n) > 5 and (n in norm_full(x) or norm_full(x) in n))
               for x in names)


def id_tokens(part):
    """Identifier-like tokens with a label in front of them (registration, tax, bank...).
    An identity card written with spaces is one token, not its first six digits."""
    toks = re.findall(r"(?<![\w\-/])\d{6}[ -]\d{2}[ -]\d{4}(?![\w\-/])|"
                      r"(?<![\w\-/])[A-Za-z]{0,4}\d[\w\-/]*\d(?:[ ]\d{3,})*(?:[ ]\(\w+-\w\))?", part)
    return [t for t in dict.fromkeys(toks) if identifier_kind(t, part, wide=True)]


def read_part(model, sysmsg, name, part, i, n, known):
    """The focused questions about one part of a document. known: parties found in the
    parts before it."""
    pre = doc_prefix(name, part, i, n)
    say("ask", "reading what it is about", n=i, of=n)
    ov = model.ask(sysmsg, pre + Q_OVERVIEW, OVERVIEW_SCHEMA, max_tokens=1500)
    say("ask", "finding who and what it names", n=i, of=n)
    parties = clean_parties(ask_safe(model, sysmsg, pre + Q_PARTIES, PARTIES_SCHEMA, 1500, "parties").get("parties"))
    names = list(dict.fromkeys(named(merge_parties(known + parties))))[:40]
    say("ask", "finding every figure and date", n=i, of=n)
    q = Q_FIGURES.format(names="; ".join(names) or "(none)")
    figures = ask_safe(model, sysmsg, pre + q, figures_schema(names), 3000, "figures").get("figures") or []
    ids = []
    if names and id_tokens(part):
        say("ask", "reading registration, tax and bank numbers", n=i, of=n)
        ids = ask_safe(model, sysmsg, pre + Q_IDS.format(names="; ".join(names)), ids_schema(names), 800,
                       "identifiers").get("identifiers") or []

    # A second look at what patterns find in the text but the answers left out.
    got = F.numbers_in(" ".join(str(f.get("value", "")) for f in figures if isinstance(f, dict)))
    fig_c = [c for c in F.figure_candidates(part) if not F.covered(c["value"], got)][:20]
    comp_c, people_c = F.party_candidates(part)
    have = [p["name"] for p in known + parties]
    party_c = [x for x in comp_c + people_c if not mentioned(x, have)][:10]
    if fig_c or party_c:
        say("ask", f"asking about {len(fig_c) + len(party_c)} more things it found", n=i, of=n)
        lines = ["These appear in the document but are not in your lists yet. Say what each one is, with "
                 "the same labels as before. Leave out anything that is only a heading, a page number or a "
                 "reference number of the document itself."]
        if fig_c:
            lines += ["", "Figures:"] + [f"- {c['value']}  (in: {c['context']})" for c in fig_c]
        if party_c:
            lines += ["", "Names:"] + [f"- {x}" for x in party_c]
        a = ask_safe(model, sysmsg, pre + "\n".join(lines),
                     second_schema(names, [c["value"] for c in fig_c], party_c), 1500, "second look")
        for f in a.get("figures") or []:
            if isinstance(f, dict) and f.get("item"):
                figures.append({"about": f.get("about"), "attribute": f.get("attribute"),
                                "qualifier": f.get("qualifier"), "value": f["item"]})
        parties += clean_parties(a.get("parties"))
    return {"overview": ov, "parties": parties, "figures": figures, "identifiers": ids}


def without_value(qualifier, value):
    """A qualifier says what a figure is for, not the figure again ("30 days from invoice
    date" for "30 days" is "from invoice date")."""
    if value and value.lower() in qualifier.lower():
        i = qualifier.lower().index(value.lower())
        qualifier = (qualifier[:i] + qualifier[i + len(value):]).strip(" ,;:-()")
    return clean_line(qualifier, 100)


def grounded_list(items, nums, limit):
    out = []
    for x in items or []:
        x = clean_line(x, limit)
        if not x:
            continue
        bad = F.ungrounded_numbers(x, nums)
        if bad:
            log(f"dropped a point with numbers the document does not contain ({', '.join(bad)}): {x[:80]}")
            continue
        out.append(x)
    return out


def merge_parts(results, body):
    """One document from its parts' answers, keeping only what the document contains."""
    sc, nums = F.canon(body), F.numbers_in(body)
    first = (results[0] if results else {}).get("overview") or {}
    m = {"title": nice_title(clean_line(first.get("title") or "", 160)), "doc_type": first.get("doc_type") or "other",
         "doc_date": first.get("doc_date") or "", "summary": [], "decisions": [], "obligations": [],
         "open_questions": [], "parties": [], "figures": [], "identifiers": [], "dropped": 0}
    if m["doc_type"] not in DOC_TYPES:
        m["doc_type"] = "other"
    if F.ungrounded_numbers(m["title"], nums):
        m["title"] = ""
    for r in results:
        ov = r.get("overview") or {}
        if not m["doc_date"] and ov.get("doc_date"):
            m["doc_date"] = ov["doc_date"]
        m["summary"] += grounded_list(ov.get("summary"), nums, 300)
        m["obligations"] += grounded_list(ov.get("obligations"), nums, 240)
        m["open_questions"] += grounded_list(ov.get("open_questions"), nums, 300)
        for dcs in ov.get("decisions") or []:
            if not isinstance(dcs, dict) or not grounded_list([dcs.get("decision")], nums, 300):
                continue
            date = dcs.get("date") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(dcs.get("date") or "")) else ""
            m["decisions"].append({"title": clean_line(dcs.get("title") or dcs.get("decision"), 90),
                                   "decision": clean_line(dcs.get("decision"), 300),
                                   "date": date if date and not F.ungrounded_numbers(date, nums) else ""})
        m["parties"] += r.get("parties") or []
        m["identifiers"] += [i for i in r.get("identifiers") or [] if isinstance(i, dict)]
        for f in r.get("figures") or []:
            if not isinstance(f, dict) or f.get("attribute") not in F.ATTRIBUTES:
                continue
            value = clean_line(f.get("value"), 120)
            if not value or not F.grounded(value, sc, nums):
                m["dropped"] += 1
                if value:
                    log(f"dropped a figure the document does not contain: {value[:60]}")
                continue
            q = without_value(clean_line(f.get("qualifier"), 100), value)
            if F.ungrounded_numbers(q, nums):
                q = ""
            attribute = f["attribute"] if F.fits(f["attribute"], value) else "other"
            if attribute in F.COMPARED and PART_PAYMENT.search(q + " " + value):
                attribute = "amount"  # "first instalment" of a fee is a payment, not the fee
            m["figures"].append({"about": clean_line(f.get("about"), 120) or THIS_DOC,
                                 "attribute": attribute, "qualifier": q, "value": value})
    m["parties"] = merge_parties(m["parties"])
    for p in m["parties"]:
        if F.ungrounded_numbers(p["role"], nums):
            p["role"] = ""
    seen_d, decisions = set(), []
    for dc in m["decisions"]:
        if F.ungrounded_numbers(dc["title"], nums):
            dc["title"] = clean_line(dc["decision"], 90)
        if F.canon(dc["title"]) not in seen_d:
            seen_d.add(F.canon(dc["title"]))
            decisions.append(dc)
    m["decisions"] = decisions
    seen, figs = set(), []
    for f in m["figures"]:
        k = (norm_full(f["about"]), f["attribute"], F.norm_value(f["attribute"], f["value"]), F.canon(f["qualifier"]))
        if k not in seen:
            seen.add(k)
            figs.append(f)
    m["figures"] = figs
    for k in ("summary", "obligations", "open_questions"):
        m[k] = list(dict.fromkeys(m[k]))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", m["doc_date"] or "") or F.ungrounded_numbers(m["doc_date"], nums):
        m["doc_date"] = ""
    return m


def ask_relations(model, sysmsg, d, pre):
    people = [p for p in d["parties"] if p["kind"] in ("company", "person", "product")]
    names = [p["name"] for p in people]
    if len(names) < 2:
        return []
    say("ask", "linking how the parties relate")
    listing = "\n".join(f"- {p['name']} ({p['kind']}): {p['role'] or 'named'}" for p in people)
    q = ((pre or f"Document: {d['title']}\n" + "\n".join(f"- {s}" for s in d["summary"]) + "\n\n")
         + "Which relationships between these parties does the document state?\n" + listing + "\n\n"
         "Answer with subject, relation and object, using the names exactly as listed: a person's place "
         "at a company (director_of, employee_of, signatory_for, contact_for), how companies deal with "
         "each other (supplier_of, customer_of, landlord_of, tenant_of, adviser_to, bank_of, ...), and "
         "who supplies a product (supplier_of). Only what the document states.")
    a = ask_safe(model, sysmsg, q, relations_schema(names), 900, "relationships")
    out, seen = [], set()
    for r in a.get("relations") or []:
        if not isinstance(r, dict) or r.get("subject") == r.get("object") or r.get("relation") not in LP.RELATIONS:
            continue
        k = (r["subject"], r["relation"], r["object"])
        if k not in seen and r["subject"] in names and r["object"] in names:
            seen.add(k)
            out.append({"subject": r["subject"], "relation": r["relation"], "object": r["object"]})
    return out


# A party's role in a document says how it deals with the business itself.
ROLE_LINKS = [
    (r"\b(supplier|vendor|supplies|seller)\b", "supplier_of", False),
    (r"\b(landlord|lessor)\b", "landlord_of", False),
    (r"\b(tax agent|accountant|auditor|advis[eo]r|consultant|lawyer|solicitor|company secretary)\b", "adviser_to", False),
    (r"\bbank\b", "bank_of", False),
]
# A person's role in a document of the business alone: a director plainly called so is
# one of its directors; a job title ("Sales Director", "manager") is one of its staff.
PERSON_LINKS = [(lambda t: wiki_menu.plain_party_role(t) and re.search(r"director|pengarah", t, re.I), "director_of"),
                (re.compile(r"\b(director|manager|officer|clerk|executive|staff|accountant|assistant)\b", re.I).search,
                 "employee_of")]


def role_relations(d):
    """Links the answers left out but the roles state: a company named as a supplier,
    landlord, adviser or bank deals with the business itself (a "customer" is left to the
    answers: a model calls a payee that too); a product has the one
    company named as its seller; in a document of the business alone, its people work there."""
    own = next((p for p in d["parties"] if p["kind"] == "company" and Run.company
                and same_entity(Run.company, p["name"])), None)
    linked = {(r["subject"], r["object"]) for r in d["relations"]} | {(r["object"], r["subject"]) for r in d["relations"]}
    out = []
    companies = [p for p in d["parties"] if p["kind"] == "company"]
    if own:
        for p in companies:
            if p is own or (p["name"], own["name"]) in linked:
                continue
            for rx, relation, reverse in ROLE_LINKS:
                if re.search(rx, p["role"] or "", re.I):
                    s_, o_ = (own["name"], p["name"]) if reverse else (p["name"], own["name"])
                    out.append({"subject": s_, "relation": relation, "object": o_})
                    break
        if len(companies) == 1:
            for p in d["parties"]:
                if p["kind"] != "person" or any(p["name"] in pair for pair in linked):
                    continue
                for says, relation in PERSON_LINKS:
                    if says(p["role"] or ""):
                        out.append({"subject": p["name"], "relation": relation, "object": own["name"]})
                        break
    sellers = [p for p in companies if re.search(r"\b(supplier|vendor|supplies|seller|sells)\b", p["role"] or "", re.I)]
    for p in d["parties"]:
        if p["kind"] == "product" and len(sellers) == 1 and not any(p["name"] in pair for pair in linked):
            out.append({"subject": sellers[0]["name"], "relation": "supplier_of", "object": p["name"]})
    return out


def topic_match(title, existing):
    """An existing topic page with (nearly) the same title as a proposed one."""
    words = lambda t: {w for w in F.WORD.findall(F.canon(t)) if w not in F.STOP}  # noqa: E731
    tok = words(title)
    best, score = None, 0.0
    for rel, t in existing:
        if slugify(t) == slugify(title):
            return rel
        other = words(t)
        if tok and other:
            s = len(tok & other) / min(len(tok), len(other))
            if s > score:
                best, score = rel, s
    return best if score >= 0.67 else None


def topic_choices(wiki):
    """The topic pages a document can add to: the wiki's own, then the usual ones it does
    not have yet. (existing, choices, titles)."""
    existing = wiki.topics()[:80]
    have = {slugify(t) for _, t in existing}
    choices = [(rel, t, rel.split("/")[1]) for rel, t in existing]
    choices += [(None, t, sec) for sec, t, _ in TOPICS if slugify(t) not in have
                and not topic_match(t, existing)]
    return existing, choices, list(dict.fromkeys(t for _, t, _ in choices))


def ask_topics(model, sysmsg, d, wiki, proj_list, pre):
    existing, choices, titles = topic_choices(wiki)
    say("ask", "choosing the topic pages it adds to")
    listing = "\n".join(f"- {t} ({SECTIONS[sec]})" for _, t, sec in choices)
    q = ((pre or f"Document: {d['title']}\n" + "\n".join(f"- {s}" for s in d["summary"]) + "\n\n")
         + "Which of these topic pages of the business's wiki should this document add to?\n\n" + listing
         + "\n\nChoose up to three that the document has something to say about. Answer \"new\" (with a short "
         "title) only for a subject none of these covers and a business keeps one page on; never a page for this "
         "one document, a company, a person or a product. For each topic, give up to 4 short points from this "
         "document that belong on that page, each a full sentence that names what it is about."
         + ("\n\nAlso choose its project folder: 'general' unless one clearly fits." if len(proj_list) > 1 else "")
         + ("\n\nAnd choose where this document belongs in the wiki's menu: one or two of these, most fitting "
            "first.\n" + wiki_menu.listing(compact=True) if wiki_menu.PATHS else ""))
    a = ask_safe(model, sysmsg, q, topics_schema(titles, proj_list), 1200, "topics")
    d["menu"] = wiki_menu.normalise(a.get("menu"))[:2]
    return settle_topics(a, d, wiki, existing, choices, proj_list)


def settle_topics(a, d, wiki, existing, choices, proj_list):
    """The topic pages and project folder from an answer, with the points the document
    contains, an existing page for a title it (nearly) has, and never a page that is the
    document itself."""
    nums = F.numbers_in(d["_body"])
    out = []
    for t in a.get("topics") or []:
        if not isinstance(t, dict):
            continue
        points = grounded_list(t.get("points"), nums, 240)
        if not points:
            continue
        if t.get("page") and t["page"] != "new":
            rel, title, sec = next(((r, x, sc) for r, x, sc in choices if x == t["page"]), (None, None, None))
            if title:
                out.append({"rel": rel, "title": title, "section": sec, "points": points})
            continue
        title = label(clean_line(t.get("title"), 80), 80)
        section = t.get("section") if t.get("section") in SECTIONS else "finance-legal"
        if not norm_full(title) or F.ungrounded_numbers(title, nums):
            continue
        if topic_match(title, [("doc", d["title"]), ("doc", d.get("_name", ""))]):
            log(f"topic {title!r} is the document itself; left out")
            continue
        rel = topic_match(title, existing + [(o["rel"], o["title"]) for o in out if o["rel"]])
        out.append({"rel": rel, "title": wiki.pages[rel]["title"] if rel in wiki.pages else title,
                    "section": section, "points": points})
    merged = {}
    for t in out:
        key = t["rel"] or ("new", slugify(t["title"]))
        if key in merged:
            merged[key]["points"] = list(dict.fromkeys(merged[key]["points"] + t["points"]))
        else:
            merged[key] = t
    project = a.get("project") if a.get("project") in proj_list else "general"
    return list(merged.values())[:3], project


def process_document(model, wiki, claims, src, sysmsg, touched):
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

    model.deadline = time.time() + Run.doc_budget
    try:
        # 1. Every question first ...
        parts = chunks_of(body)
        truncated = len(parts) > MAX_PARTS
        parts = parts[:MAX_PARTS]
        results, known = [], []
        for i, part in enumerate(parts, 1):
            if len(parts) > 1:
                wiki_events.emit("progress", file=src, msg=f"Reading {name}, part {i} of {len(parts)}")
            r = read_part(model, sysmsg, name, part, i, len(parts), known)
            known = merge_parties(known + r["parties"])
            results.append(r)
        d = merge_parts(results, body)
        d["_body"], d["_name"] = body, os.path.splitext(name)[0]
        if d["doc_type"] not in DECISION_TYPES or not DECISION_CUE.search(body):
            d["decisions"] = []  # terms of an invoice or a contract are facts, not decisions
        if len(results) > 1:
            say("ask", f"condensing the {len(results)} parts into one summary")
            points = "\n".join(f"- {p}" for p in d["summary"])
            c = ask_safe(model, sysmsg, f"These are summary points from the parts of one document, {name}.\n"
                         "Write its title and a summary of at most 10 points.\n\n" + points,
                         CONDENSE_SCHEMA, 1200, "condensing")
            nums = F.numbers_in(body)
            if c.get("title") and not F.ungrounded_numbers(c["title"], nums):
                d["title"] = clean_line(c["title"], 160)
            d["summary"] = grounded_list(c.get("summary"), nums, 300) or d["summary"]
        pre = doc_prefix(name, parts[0], 1, 1) if len(parts) == 1 else None
        d["relations"] = ask_relations(model, sysmsg, d, pre)
        d["relations"] += role_relations(d)
        d["relations"] = [r for r in d["relations"] if F.relation_stated(r["relation"], body) and (
            Run.company and (same_entity(Run.company, r["subject"]) or same_entity(Run.company, r["object"]))
            or share_a_row(r["subject"], r["object"], body))]
        d["topics"], project = ask_topics(model, sysmsg, d, wiki, projects(), pre)
        dest = choose_dest(f"raw/{project}", name, src)
        plan = plan_document(model, wiki, claims, sysmsg, d, dest, body)
    finally:
        model.deadline = None

    # 2. ... then the writes, which need no model and cannot half-fail on a slow answer.
    say("write", "writing its pages")
    write_document(wiki, claims, src, dest, project, d, plan, truncated, len(parts), touched)
    say("file", f"filing it in {os.path.dirname(dest)}")
    place(src, dest)
    wiki_review.relocate(src, dest)   # its review items now name where it is
    wiki_events.emit("filed", file=src, to=dest)
    log(f"filed {src} -> {dest}" + (f" ({d['dropped']} figure(s) not in the document left out)" if d["dropped"] else ""))
    log_line("intake", project, d["title"] or name)
    return True


# ============================================================== Claude reads =====
# The fast pipeline (engine "claude" with "pipeline": "fast"): Claude answers every question
# about a document in one call (scripts/wiki_claude.py), many documents at once. What
# follows the answer is the same as for the model on this Mac: grounding, the claims, the
# contradictions, the pages and the filing are code.
CLAUDE_DOC_CHARS = 150000   # about 40k tokens; a longer document is read from its start
MAX_FAILED_IN_A_ROW = 5     # documents failing one after another with an API failure that is
                            # not about the document (wiki_claude.service_failure), nothing
                            # answered in the run: Claude, not the documents
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
LOOK_NOTES = {"scanned-no-ocr", "image-no-ocr", "image-little-text", "heic-not-converted"}


class ClaudeModel:
    """Claude behind the Model interface, so the questions the code asks one at a time (the
    figures on a page a person wrote, a page's overview) go to Claude too."""

    def __init__(self, cfg):
        self.claude = wiki_claude.Claude(model=cfg.get("model") or "", budget=float(cfg.get("budget") or 2.5),
                                         timeout=float(cfg.get("timeout") or 900))
        self.deadline = None
        self.stats = self.claude.stats

    def ask(self, system, user, schema, max_tokens=1500, read_dirs=(), kind="other"):
        """kind: "read" (a document), "overview" (a page's overview) or "other". Each kind
        can have its own model and effort (Run.models, Run.efforts: wiki.config.json "fast")."""
        try:
            return self.claude.ask(system, user, schema, read_dirs=read_dirs, kind=kind,
                                   model=Run.models.get(kind), effort=Run.efforts.get(kind))
        except wiki_claude.ClaudeUnavailable as e:
            raise ModelUnavailable(f"Claude could not run: {e}")

    def stop(self):
        self.claude.stop()


def extract_schema(titles, proj_list):
    return obj({
        "title": text(160), "doc_type": S("string", enum=DOC_TYPES), "doc_date": text(10),
        "summary": S("array", items=text(300)),
        "decisions": S("array", items=obj({"title": text(90), "decision": text(300), "date": text(10)})),
        "obligations": S("array", items=text(240)),
        "open_questions": S("array", items=text(300)),
        "parties": S("array", items=obj({"name": text(120), "kind": S("string", enum=KINDS), "role": text(160)})),
        "figures": S("array", items=obj({"about": text(120), "attribute": S("string", enum=F.ATTRIBUTES),
                                          "qualifier": text(100), "value": text(120)})),
        "identifiers": S("array", items=obj({"entity": text(120), "kind": S("string", enum=ID_KINDS),
                                              "value": text(80)})),
        "relations": S("array", items=obj({"subject": text(120), "relation": S("string", enum=LP.RELATIONS),
                                            "object": text(120)})),
        "topics": S("array", items=obj({"page": S("string", enum=titles + ["new"]),
                                         "section": S("string", enum=list(SECTIONS)), "title": text(80),
                                         "points": S("array", items=text(240))})),
        "project": S("string", enum=proj_list),
        "photo": text(1500),
        **menu_field(),
    })


def menu_field():
    """The reader's choice of where a document belongs in the wiki's menu (none when the
    menu cannot be read: engine/menu.json)."""
    return {"menu": S("array", items=S("string", enum=wiki_menu.PATHS), maxItems=2)} if wiki_menu.PATHS else {}


def menu_question():
    """The line asking for it, with the menu's categories (compact: one line a section)."""
    if not wiki_menu.PATHS:
        return ""
    return ("- menu: where this document belongs in the wiki's menu: one or two of these (most fitting first):\n"
            + wiki_menu.listing(compact=True) + "\n")


Q_EXTRACT = """Read the whole document above and answer every part.

- title: as a reader would name it, with its number if it has one. doc_type. doc_date: YYYY-MM-DD, empty if it has none.
- summary: up to 10 points that carry the key names, amounts and dates. decisions it records (a short title, the decision in one sentence, its date); obligations it sets (who must do what, by when); open_questions it leaves.
- parties: every company, organisation, person and product or service it names, and each place that matters to the business (a site, an outlet, an office). The name exactly as written (a company's full legal name when the document gives it; a person's name without Mr or Ms), its kind, and its role in this document: supplier, buyer, customer, landlord, tax agent, director, signatory, contact, product bought, ...
- figures: every amount, price, fee, quantity, percentage, date, deadline, duration, rate and address it states, and a person's salary, date of birth, phone number and email, one entry per figure, table rows included. about: one of your party names exactly as you wrote it, or "this document" for the document's own numbers, totals and dates. attribute: what kind of figure it is (payment_terms is only the time a buyer has to pay; a notice to end or change something is notice_period; how long an agreement runs is contract_term; a charge for a service is fee; a person's pay is salary). qualifier: a few words saying exactly what it is for. value: copied exactly as written, with its currency or unit.
- identifiers: registered names, company or registration numbers, tax numbers (TIN, SST, GST), licence numbers, bank account numbers, and a person's identity card (IC, NRIC, MyKad) or passport, EPF (KWSP) and SOCSO (PERKESO) numbers: the party they belong to (entity: one of your party names) and the value copied exactly.
- relations: the relationships between your parties that the document states: a person's place at a company (director_of, employee_of, signatory_for, contact_for), how companies deal with each other (supplier_of, customer_of, landlord_of, tenant_of, adviser_to, bank_of, ...), who supplies a product (supplier_of). Names exactly as in your parties list.
- topics: up to three of these topic pages of the business's wiki that the document has something to say about, each with up to 4 points from it that belong on that page, each a full sentence that names what it is about:
{listing}
  Answer "new" (with a short title and its section) only for a subject none of these covers and a business keeps one page on; never a page for this one document, a company, a person or a product.
- project: its project folder, one of: {projects}. "general" unless one clearly fits.
{menu}- photo: only when the document is a photo or picture: a plain, factual description of what it shows (the scene or place, objects, products, equipment, quantities, condition, any documents or screens in view, any text, quoted). Describe people and never name anyone from their face: a name may come only from text in the picture. Empty for any other document.

Report only what the document states and leave a field empty when it does not say. Never guess and never add outside knowledge."""

LOOK_AT = ("\nThe text above was converted from a photo or a scan and may be incomplete or garbled. Look at "
           "the original with the Read tool: {path} (read only that file), and answer from what you see.\n")


# ------------------------------------------------------------ which reader -----
# Off by default ("fast": {"routing": {"enabled": true}} turns it on): a lighter, cheaper
# reader ("fastModel", Haiku) for the documents it can read fully, the reader's own model
# (Sonnet) for the rest. Code sorts the clear cases and sends everything else to the
# reader's own model; with "jev": true, Jev (TypeSafe) judges those others from an excerpt
# instead (off by default: in a real gold-set run it never found one the lighter reader
# could take). Code checks every answer of the lighter reader and has a thin one read again
# by the reader's own model. A wrong guess costs a second read, never a worse page.
ROUTING_DEFAULTS = {"enabled": False, "fastModel": "haiku", "jev": False, "jevBar": 0.7,
                    "maxFastChars": 20000, "simpleChars": 6000, "minCoverage": 0.5}
SIMPLE_DOC_RE = re.compile(r"(?i)\b(tax invoice|invoice|receipt|quotation|quote|purchase order|"
                           r"delivery order|delivery note|credit note|debit note|payment voucher)\b")
CONTRACT_NAME_RE = re.compile(r"(?i)(agreement|contract|tenancy|lease|\bmou\b|memorandum|\bdeed|terms)")
CONTRACT_RE = re.compile(r"(?i)\b(whereas|hereinafter|hereby agree|the parties agree|in witness whereof|"
                         r"this agreement|this contract|tenancy agreement|memorandum of understanding)\b")
SHEET_EXT = {".xlsx", ".xlsm", ".xls", ".ods", ".csv", ".numbers"}
TABLE_LINES = 60     # more table rows than this: a table-heavy document, for the full reader
JEV_ROUTE_Q = ("Will a fast, lighter reader capture every figure, date, identifier and party in this "
               "document fully and correctly, so that a stronger reader is not needed?")
_jev_routing = threading.BoundedSemaphore(4)   # wiki_jev.PARALLEL requests at once


def routing_settings(fast, config):
    """The "routing" settings under "fast", with the defaults; the TypeSafe key in "jev_key"
    when Jev may judge in this run, else ""."""
    got = fast.get("routing") if isinstance(fast.get("routing"), dict) else {}
    s = dict(ROUTING_DEFAULTS)
    for k, v in got.items():
        want = type(s.get(k))
        if k not in s or isinstance(v, bool) != (want is bool):
            continue
        if want is str and isinstance(v, str) and v.strip():
            s[k] = v.strip()
        elif want in (int, float) and isinstance(v, (int, float)) and v >= 0:
            s[k] = want(v)
        elif want is bool:
            s[k] = v
    s["jev_key"] = ""
    if s["enabled"] and s["jev"]:
        try:
            import wiki_jev
            if wiki_jev.available(config):
                s["jev_key"] = wiki_jev.get_key() or ""
        except Exception as e:
            log(f"Jev will not choose readers: {e}")
    return s


def routed(what):
    with Run.routed_lock:
        Run.routed[what] = Run.routed.get(what, 0) + 1


def route_by_code(name, body, look, truncated):
    """("full" | "fast" | "ask", why) from what code can see before reading: photos, scans,
    long, table-heavy documents and contracts go to the full reader; a short invoice,
    receipt, quotation or order goes to the lighter one; the rest is for Jev."""
    s = Run.routing
    if look:
        return "full", "a photo or a scan"
    if truncated or len(body) > s["maxFastChars"]:
        return "full", "long"
    head = name + "\n" + body[:3000]
    if CONTRACT_NAME_RE.search(name) or CONTRACT_RE.search(body[:5000]):
        return "full", "a contract or formal document"
    if sum(1 for l in body.splitlines() if l.count("|") >= 3) > TABLE_LINES:
        return "full", "many table rows"
    # A spreadsheet that names invoices (a ledger, a listing) is not an invoice: Jev judges it.
    if (len(body) <= s["simpleChars"] and SIMPLE_DOC_RE.search(head)
            and os.path.splitext(name)[1].lower() not in SHEET_EXT):
        return "fast", "a short invoice, receipt, quotation or order"
    return "ask", ""


def jev_route(name, body):
    """Jev's probability that the lighter reader gets this document fully right; None on any
    failure. Jev sees the file name, a few counts and the document's start."""
    import wiki_jev
    _, nums = F.numbers_in(body)
    tables = sum(1 for l in body.splitlines() if l.count("|") >= 3)
    state = (f"Document file name: {name}\nLength: {len(body)} characters, {len(body.splitlines())} lines, "
             f"{tables} table rows, {len(nums)} distinct numbers.\nThe document's start:\n"
             f"{body[:wiki_jev.DOC_CHARS]}")
    try:
        with _jev_routing:
            out = wiki_jev.call(state, {"q": {"type": "noul", "instructions": JEV_ROUTE_Q}}, Run.routing["jev_key"])
        p = (out["answers"].get("q") or {}).get("noul")
    except Exception as e:
        log(f"Jev could not choose a reader for {name}: {e}")
        return None
    return float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else None


def choose_reader(name, body, look, truncated):
    """"fast" or "full" for one document, counted in Run.routed."""
    where, why = route_by_code(name, body, look, truncated)
    if where != "ask":
        routed(f"code-{where}")
        return where, why
    if not Run.routing.get("jev_key"):
        routed("code-full")
        return "full", "no one to judge it"
    p = jev_route(name, body)
    if p is None:
        routed("jev-error")
        return "full", "Jev could not judge it"
    where = "fast" if p >= Run.routing["jevBar"] else "full"
    routed(f"jev-{where}")
    return where, f"Jev {p:.2f}"


def routing_summary():
    """One line for the runner log: where the documents went, and how many the lighter
    reader had to leave to a second read (above about a quarter, it stops saving money)."""
    r = Run.routed
    fast = r.get("code-fast", 0) + r.get("jev-fast", 0)
    full = r.get("code-full", 0) + r.get("jev-full", 0) + r.get("jev-error", 0)
    again = r.get("reread", 0)
    return (f"{fast} to the lighter reader (code {r.get('code-fast', 0)}, Jev {r.get('jev-fast', 0)}), "
            f"{full} to the full reader (code {r.get('code-full', 0)}, Jev {r.get('jev-full', 0)}, "
            f"Jev failed {r.get('jev-error', 0)}), {again} read again"
            + (f" ({again / fast:.0%} of the lighter reader's)" if fast else ""))


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def thin_answer(a, body):
    """Why the lighter reader's answer looks incomplete ("" when it does not): no summary,
    no parties for a document with text, or too few of the document's numbers and dates
    anywhere in the answer."""
    if not a.get("summary"):
        return "no summary"
    if len(body.strip()) > 200 and not a.get("parties"):
        return "no parties"
    dates, nums = F.numbers_in(body)
    wanted = dates | {n for n in nums if len(n.replace(".", "")) >= 3}
    if len(wanted) >= 4:
        got_d, got_n = F.numbers_in("\n".join(_strings(a)))
        share = len(wanted & (got_d | got_n)) / len(wanted)
        if share < Run.routing["minCoverage"]:
            return f"{share:.0%} of its numbers"
    return ""


def claude_read(model, src, sysmsg, listing, titles, proj_list):
    """Claude's one answer about one document. Runs in a worker thread: it reads files and
    asks, and never writes anything."""
    name = os.path.basename(src)
    mirror = mirror_path(src)
    if not os.path.isfile(os.path.join(WIKI_DIR, mirror)):
        return {"src": src, "skip": "not converted yet"}
    if Run.cap and model.claude.spent() >= Run.cap:
        return {"src": src, "skip": f"the batch reached its spending cap (${Run.cap:g})", "capped": True}
    fm, body = split_frontmatter(read(mirror))
    method, note = fm_get(fm, "method"), fm_get(fm, "note")
    ext = os.path.splitext(name)[1].lower()
    picture = ext in IMAGE_EXT
    look = picture or note in LOOK_NOTES or not body.strip()
    if method == "ERROR" and not (picture or ext == ".pdf"):
        return {"src": src, "unreadable": f"conversion said {method} {note}".strip()}
    target = os.path.join(WIKI_DIR, src)
    preview = fm_get(fm, "preview")
    if preview and os.path.isfile(os.path.join(WIKI_DIR, "cache", "md", preview)):
        target = os.path.join(WIKI_DIR, "cache", "md", preview)   # a HEIC photo's JPEG copy
    truncated = len(body) > CLAUDE_DOC_CHARS
    instructions = Q_EXTRACT.format(listing=listing, projects=", ".join(proj_list), menu=menu_question())
    user = (f"Document file name: {name}\n<document>\n{body[:CLAUDE_DOC_CHARS]}\n</document>\n"
            + (f"\nOnly the start of this long document is shown ({CLAUDE_DOC_CHARS} characters).\n" if truncated else ""))
    if Run.instructions_in_system:
        # The same instructions for every document of the run, in the system prompt: the part
        # of each call that Claude can read from its cache instead of paying for it again.
        sysmsg = sysmsg + "\n\nHow to read each document you are given:\n" + instructions.replace(
            "the whole document above", "the whole document")
        user += (LOOK_AT.format(path=target) if look else "") + "\nAnswer every part of the reading instructions."
    else:
        user += (LOOK_AT.format(path=target) if look else "") + "\n" + instructions
    wiki_events.emit("read", file=src)
    log(f"reading {src} (Claude)")
    schema, dirs = extract_schema(titles, proj_list), [os.path.dirname(target)] if look else ()
    a = None
    if Run.routing.get("enabled"):
        where, why = choose_reader(name, body, look, truncated)
        if where == "fast":
            log(f"{src}: the lighter reader ({why})")
            try:
                a = model.ask(sysmsg, user, schema, read_dirs=dirs, kind="read-fast")
                thin = thin_answer(a, body)
            except ModelUnavailable:
                raise
            except Exception as e:
                thin = f"it failed: {e}"
            if thin:
                log(f"{src}: the lighter reader's answer looks thin ({thin}); reading it again")
                routed("reread")
                a = model.ask(sysmsg, user, schema, read_dirs=dirs, kind="reread")
        else:
            log(f"{src}: the full reader ({why})")
    if a is None:
        a = model.ask(sysmsg, user, schema, read_dirs=dirs, kind="read")
    if (not (a.get("parties") or a.get("figures") or a.get("photo")) and len(a.get("summary") or []) < 2
            and len(body.strip()) > 200):
        # An answer with nothing in it, for a document with text: it happens now and then
        # (a large table), and a second ask nearly always gets the real answer. A skeleton
        # with one summary line and no parties or figures counts as empty: after a malformed
        # first answer Claude sometimes sends just that.
        log(f"{src}: Claude's answer was empty; asking again")
        a = model.ask(sysmsg, user, schema, read_dirs=dirs, kind="read")
    return {"src": src, "answer": a, "body": body, "truncated": truncated, "picture": picture,
            "taken": fm_get(fm, "taken")}


def claude_document(model, wiki, claims, got, sysmsg, touched):
    """Claude's answer about one document, checked and written like the local model's."""
    src = got["src"]
    Run.current = src
    name = os.path.basename(src)
    if got.get("skip"):
        log(f"{src}: {got['skip']}; left for the next run")
        return False
    if got.get("unreadable"):
        review("general", src, "could not be read", [
            f"{name}: {got['unreadable']}. It stays in the queue; check that it opens."])
        return False
    a, body = got["answer"], got["body"]
    photo = clean_line(a.get("photo"), 1500) if got.get("picture") or not body.strip() else ""
    parties = merge_parties(clean_parties(a.get("parties")))

    def ref(x):
        """One of the document's parties, by any spelling the answer used for it."""
        n = norm_full(x)
        if not n:
            return None
        for p in parties:
            if n == norm_full(p["name"]) or any(n == norm_full(v) for v in p.get("variants", [])):
                return p["name"]
        hits = [p["name"] for p in parties if p["kind"] == "company" and same_entity(x, p["name"])]
        return hits[0] if len(hits) == 1 else None

    figures = []
    for f in a.get("figures") or []:
        if isinstance(f, dict):
            about = THIS_DOC if norm_full(f.get("about")) in ("", norm_full(THIS_DOC)) else ref(f.get("about"))
            figures.append(dict(f, about=about or THIS_DOC))
    ids = [dict(i, entity=ref(i.get("entity"))) for i in a.get("identifiers") or []
           if isinstance(i, dict) and ref(i.get("entity"))]
    overview = {k: a.get(k) for k in ("title", "doc_type", "doc_date", "summary", "decisions", "obligations",
                                      "open_questions")}
    # A photo's summary may say what the picture shows; its figures must still be in its text.
    d = merge_parts([{"overview": overview, "parties": parties, "figures": [], "identifiers": ids}],
                    body + ("\n" + photo if photo else ""))
    checked = merge_parts([{"overview": {}, "parties": [], "figures": figures, "identifiers": []}], body)
    d["figures"], d["dropped"] = checked["figures"], checked["dropped"]
    d["_body"], d["_name"], d["photo"] = body, os.path.splitext(name)[0], photo
    d["taken"] = clean_line(got.get("taken"), 20) if photo else ""
    if d["doc_type"] not in DECISION_TYPES or not DECISION_CUE.search(body):
        d["decisions"] = []
    names = named(d["parties"])
    rels, seen = [], set()
    for r in a.get("relations") or []:
        if not isinstance(r, dict) or r.get("relation") not in LP.RELATIONS:
            continue
        s_, o_ = ref(r.get("subject")), ref(r.get("object"))
        if s_ and o_ and s_ != o_ and s_ in names and o_ in names and (s_, r["relation"], o_) not in seen:
            seen.add((s_, r["relation"], o_))
            rels.append({"subject": s_, "relation": r["relation"], "object": o_})
    d["relations"] = rels
    d["relations"] += role_relations(d)
    d["relations"] = [r for r in d["relations"] if F.relation_stated(r["relation"], body) and (
        Run.company and (same_entity(Run.company, r["subject"]) or same_entity(Run.company, r["object"]))
        or share_a_row(r["subject"], r["object"], body))]
    existing, choices, _ = topic_choices(wiki)
    d["topics"], project = settle_topics({"topics": a.get("topics"), "project": a.get("project")}, d, wiki,
                                         existing, choices, projects())
    d["menu"] = wiki_menu.normalise(a.get("menu"))[:2]
    for t in d["topics"]:   # a usual topic an earlier document of this run has just created
        if not t["rel"]:
            t["rel"] = topic_match(t["title"], wiki.topics())
    dest = choose_dest(f"raw/{project}", name, src)
    model.deadline = time.time() + Run.doc_budget
    try:
        plan = plan_document(model, wiki, claims, sysmsg, d, dest, body)
    finally:
        model.deadline = None
    say("write", "writing its pages")
    write_document(wiki, claims, src, dest, project, d, plan, got.get("truncated"), 1, touched)
    say("file", f"filing it in {os.path.dirname(dest)}")
    place(src, dest)
    wiki_review.relocate(src, dest)   # its review items now name where it is
    m_src, m_dest = os.path.join(WIKI_DIR, mirror_path(src)), os.path.join(WIKI_DIR, mirror_path(dest))
    if os.path.isfile(m_src) and not os.path.exists(m_dest):   # its text goes with it: no second conversion
        os.makedirs(os.path.dirname(m_dest), exist_ok=True)
        os.replace(m_src, m_dest)
    wiki_events.emit("filed", file=src, to=dest)
    log(f"filed {src} -> {dest}" + (f" ({d['dropped']} figure(s) not in the document left out)" if d["dropped"] else ""))
    log_line("intake", project, d["title"] or name)
    return True


def read_with_claude(model, wiki, claims, docs, sysmsg, touched):
    """Every document: Claude reads up to Run.lanes at once; each answer is checked and
    written as soon as it arrives, one at a time. The first document is read alone, so a
    sign-in that does not work stops the run after one call. True if no file failed."""
    ok = True
    existing, choices, titles = topic_choices(wiki)
    listing = "\n".join(f"- {t} ({SECTIONS[sec]})" for _, t, sec in choices)
    proj_list = projects()
    total = len(docs)

    # Failures that say the service is failing (no connection, a server error, a sign-in)
    # stop the run in wiki_claude.Claude.ask: ClaudeUnavailable, no try counted. What
    # reaches handle() as an exception is a failure about one document, unless its words
    # are new: service_in_a_row counts the API failures in a row that are not about the
    # document (wiki_claude.service_failure); answered, whether this run had any answer.
    service_in_a_row, capped, answered = 0, False, False

    def answer(fut):
        """A finished read's answer, or the exception it ended with."""
        try:
            return fut.result()
        except ModelUnavailable:
            raise
        except Exception as e:
            return e

    def handle(src, got):
        """One document's answer (or the exception its read ended with): checked and
        written. Returns True if Claude was asked."""
        nonlocal ok, service_in_a_row, capped, answered
        Run.current = src
        if isinstance(got, Exception):  # one bad file never stops the others ...
            e = got
            ok = False
            log(f"{src}: {e}")
            wiki_events.emit("error", file=src, msg=f"{os.path.basename(src)}: {e}")
            if str(e).startswith("Claude: ") and wiki_claude.service_failure(str(e)):
                service_in_a_row += 1
            else:
                service_in_a_row = 0
            if service_in_a_row >= MAX_FAILED_IN_A_ROW and not answered:
                # ... but the API failing document after document, in a way that is not
                # about any one document (no status, or a server's), with nothing answered
                # in this run, is Claude, not the files, in words wiki_claude does not know
                # yet: the run stops and counts no try. Any other failure counts a try for
                # its document (after two it is set aside), and the reading goes on: a
                # queue that starts with documents Claude cannot read (huge scans, a prompt
                # too long) must not stop every run before the rest.
                raise ModelUnavailable(f"Claude failed {service_in_a_row} documents in a row; the last: {e}")
            return True
        if got.get("capped"):
            Run.capped.add(src)   # never put to Claude: it must not count a try
            if not capped:
                capped = True
                log(f"stopped at its spending cap (${Run.cap:g}); the documents not read yet wait for the next run")
        asked = "answer" in got
        if asked:
            service_in_a_row, answered = 0, True
        try:
            if not claude_document(model, wiki, claims, got, sysmsg, touched) and not got.get("skip"):
                ok = False
        except ModelUnavailable:
            raise
        except Exception as e:
            ok = False
            log(f"{src}: {e}")
            wiki_events.emit("error", file=src, msg=f"{os.path.basename(src)}: {e}")
        return asked

    def reading(src):
        """claude_read in a worker thread, with the document noted as in flight until its
        answer has been handled."""
        in_flight(src, True)
        return claude_read(model, src, sysmsg, listing, titles, proj_list)

    # Alone until one document has really been put to Claude: a sign-in that does not work
    # stops the run after that one call. Documents that were being read when this script
    # died in an earlier run (Run.alone) go first, each alone: if one of them makes it die
    # again, it alone is to blame, not the documents read beside it.
    rest, done, asked_any = [d for d in docs if d in Run.alone] + [d for d in docs if d not in Run.alone], 0, False
    while rest and not (asked_any and rest[0] not in Run.alone):
        src = rest.pop(0)
        done += 1
        Run.current = src
        say("read", "Claude is reading the first document" if not asked_any else
            "Claude is reading it alone, to be safe", n=done, of=total)
        try:
            got = reading(src)
        except ModelUnavailable:
            raise
        except Exception as e:
            got = e
        asked_any = handle(src, got) or asked_any
        in_flight(src, False)
    if not rest:
        return ok
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=Run.lanes)
    try:
        futs = {pool.submit(reading, src): src for src in rest}
        waiting = set(futs)
        while waiting:
            Run.current = ""
            say("read", f"Claude is reading {min(Run.lanes, len(waiting))} documents at once", n=done, of=total)
            finished, waiting = concurrent.futures.wait(waiting, timeout=30,
                                                        return_when=concurrent.futures.FIRST_COMPLETED)
            for fut in finished:
                done += 1
                handle(futs[fut], answer(fut))
                in_flight(futs[fut], False)
    except ModelUnavailable:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return ok


# The label a document prints just before a company's identifier decides what it is; the
# model's own label is only a hint (a small model files an SST number as a registration
# number).
ID_LABELS = [
    ("sst_no", r"\b(sst|sales\s*(and|&)\s*services?\s*tax|service\s*tax)(?!\w)"),
    ("tin_no", r"\b(tin|tax\s*(identification|id|ref(erence)?|no|number|file))(?!\w)"),
    ("bank_account", r"\b(account|a/c|acct?\.?\s*no|bank)(?!\w)"),
    ("licence_no", r"\b(licen[cs]e|permit)(?!\w)"),
    ("registration_no", r"\b(company|co\.|roc|ssm|business|registration|reg\.|incorporat\w*)(?!\w)"),
]
# The labels of a person's own numbers: an identity card or passport ("NRIC", "I.C.", "No.
# K.P. Baru", "Identity Card"), EPF (KWSP) and SOCSO (PERKESO). They keep such a number
# off a company's card and out of the figures; they never decide a person's numbers.
IC_WORDS = (r"\b(n\.?\s*r\.?\s*i\.?\s*c|i\s*[./]?\s*c|identity\s+card|my\s*kad|passport|pasport|k\s*[./]\s*p|"
            r"kad\s+pengenalan)(?!\w)")
PERSON_LABELS = [("id_no", IC_WORDS), ("epf_no", r"\b(epf|kwsp)(?!\w)"), ("socso_no", r"\b(socso|perkeso)(?!\w)")]
# A person's numbers come from the reader alone, with the kind it gave: the label words of
# real documents are too many to require (aligned columns, tables, a label on the line
# above, "No. K.P. Baru", "EPF (Pekerja)"). Only explicit counter-evidence drops one: the
# label right before the number naming the employer or a company ("Employer EPF No").
# This is safe enough because an Info card is never overwritten: a number that differs
# from the one on the card goes to the review queue (merge_ids), so a reader's slip shows.
PERSON_KEEPS = ("id_no", "epf_no", "socso_no", "tin_no", "bank_account")
EMPLOYER_LABEL = re.compile(r"\b(employer|majikan|syarikat|company)|\bco\.", re.I)
_DASHES = r"\-‐‑‒–—"   # inside a [...] class: hyphens and dashes


def bare(value):
    """A number without its spaces, dashes and dots: "900101 14 5678" and "900101.14.5678"
    are the same card."""
    return re.sub(rf"[\s.{_DASHES}]+", "", value or "")


def ic_shaped(value):
    """Whether a number has a MyKad's shape: YYMMDD (a real date), the place of birth, four
    digits. Written without its dashes, twelve digits starting 19 or 20 are a company's
    number (its year of incorporation first), not a card."""
    v = bare(value)
    if not re.fullmatch(r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{6}", v):
        return False
    return ic_written(value) or v[:2] not in ("19", "20")


def ic_written(value):
    """Whether a number is written as a MyKad is, in three groups: never a company's."""
    return bool(re.fullmatch(rf"\s*\d{{6}}[\s.{_DASHES}]+\d{{2}}[\s.{_DASHES}]+\d{{4}}\s*", value or ""))


def spots(value, body):
    """Where the document prints `value` (spaces, dashes and dots aside) as a number, not as
    an amount: a currency code as a word right before a short or decimal figure ("RM
    1170", "MYR 990.00"), or ".00" after one ("6930.00"); and "11.70" never prints 1170.
    An account number after "Farm" or "MYR" is still an account number. [] when it is not
    printed as a number at all."""
    raw = re.sub(r"\s+", "", value or "")
    v = bare(value)
    if len(v) < 4 or sum(c.isdigit() for c in v) < 4:
        return []  # "SDN BHD" is not a number
    if (re.match(r"(?i)(?:rm|myr|usd|sgd|us\$|s\$|\$)", raw) or re.fullmatch(r"[\d,]*\d\.\d\d", raw)
            or re.fullmatch(r"\d{1,3}(?:,\d{3})+", raw)):
        return []  # "RM 385.00", "1,170": an amount, not a number that names anyone
    figure = v.isdigit() and len(v) <= 7
    pattern = r"(?<!\d)" + rf"[\s.{_DASHES}]*".join(re.escape(c) for c in v) + r"(?!\d)"
    out = []
    for m in re.finditer(pattern, body, re.I):
        if "." not in raw and re.search(r"\.\d{2}$", m.group(0)):
            continue  # "11.70" is an amount, never the number 1170
        decimals = re.match(r"\.\d{2}(?!\d)|,\d{3}(?!\d)", body[m.end():m.end() + 5])
        currency = re.search(r"(?i)(?<![a-z])(?:rm|myr|usd|sgd)\s*$|\$\s*$", body[max(0, m.start() - 12):m.start()])
        if figure and (decimals or currency):
            continue  # this mention is an amount: another may still be the number
        out.append(m)
    return out


def _phrase(body, start):
    """The label right before a place in the document, as a company's number is read: the
    words on its line back to the previous value or gap ("SST No: X  TIN: Y": Y is the
    TIN). Within one label the order of the lists decides ("SST Registration No")."""
    window = body[max(0, start - 48):start]
    window = window.split("\n")[-1] if "\n" in window[-30:] else window
    return re.split(r"\s{2,}|[;|]|\b[\w-]*\d[\w-]*\b", window)[-1]


def _cell_label(body, start):
    """A looser reading, only to tell whether a part may hold numbers worth asking the
    local model about: the nearest stretch with letters across a column gap or a table
    cell ("EPF No      : x", "| EPF No | x |"), or the short line above a value that
    starts its line ("EPF No" over "20481234")."""
    line_start = body.rfind("\n", 0, start) + 1
    text = re.split(r"\b[\w-]*\d[\w-]*\b", body[line_start:start])[-1]
    for seg in reversed(re.split(r"\s{2,}|\t|[;|]", text[-48:])):
        if re.search(r"[^\W\d_]", seg):
            return seg
    if not re.search(r"[^\W_]", body[line_start:start]):
        return _line_above(body, line_start)
    return ""


def _line_above(body, line_start):
    """The short line above a line, when it holds no figures (a label over its value)."""
    above = [l for l in body[:line_start].split("\n") if l.strip()][-1:]
    return above[0].strip() if above and len(above[0].strip()) <= 40 and not re.search(r"\d", above[0]) else ""


# A label's own words that name a number ("No", "A/C", an identity card): a bracket that
# holds none of them only qualifies the label before it ("Employer EPF No. (KWSP):").
NUMBER_WORD = re.compile(rf"(?:\b(?:no|number|nombor|num|a/c|acc|acct|account|akaun)\b|#|{IC_WORDS})", re.I)


def _own_label(body, start):
    """The label that is a number's own, where an employer's or a company's word makes it
    theirs: after the previous value, a field's comma or the previous field's colon, with
    what a bracket adds to it ("Company Bank A/C (Maybank):"), but not the sentence before
    a bracketed label ("The Company offers Ram Gurung (Passport No.: x)"); for a value that
    starts its line, the short line above too."""
    line_start = body.rfind("\n", 0, start) + 1
    text = re.split(r"\b[\w-]*\d[\w-]*\b", body[line_start:start])[-1]
    text = re.split(r",\s", text)[-1]
    parts = text.split(":")
    if len(parts) >= 3:   # "Company: Harbourline Bank A/C: x": the last label
        text = ":".join(parts[-2:])
    segs = [seg for seg in re.split(r"\s{2,}|\t|[;|(]", text[-48:]) if re.search(r"[^\W\d_]", seg)]
    own = segs[-1] if segs else ""
    if len(segs) > 1 and not NUMBER_WORD.search(own):
        own = f"{segs[-2]} ({own}"   # "(KWSP):" qualifies "Employer EPF No." before it
    if not re.search(r"[^\W_]", body[line_start:start]):
        own = (_line_above(body, line_start) + " " + own).strip()
    return own


def id_phrases(value, body, wide=False):
    """The label in front of each place the document prints `value` as a number (spots);
    wide: read as _cell_label does, only to decide whether to ask about a part."""
    return [(_cell_label if wide else _phrase)(body, m.start()) for m in spots(value, body)]


def _kinds(phrase, labels):
    return [kind for kind, rx in labels if re.search(rx, phrase, re.I)]


def identifier_kind(value, body, wide=False):
    """The kind of identifier `value` is by the label in front of it, a person's or a
    company's, or None: such a number is never a figure. wide: see id_phrases."""
    for phrase in id_phrases(value, body, wide):
        kinds = _kinds(phrase, PERSON_LABELS + ID_LABELS)
        if kinds:
            return kinds[0]
    return None


def company_id_kind(value, body, fund=False):
    """The kind of company identifier `value` is (registration, tax, SST, licence, bank
    account), by the label in front of it, or None. A person's number is none of them: a
    card written as one, a label naming EPF or SOCSO ("EPF Account No"), or an identity
    card with the value in its shape under "NRIC/Company No.", never gives a company's.
    fund: the party is the EPF or SOCSO itself, whose own account its name labels ("KWSP's
    account no.")."""
    if ic_written(value):
        return None
    for phrase in id_phrases(value, body):
        kinds = _kinds(phrase, ID_LABELS)
        if not kinds:
            continue
        person = _kinds(phrase, PERSON_LABELS)
        if ({"epf_no", "socso_no"} & set(person) and not fund) or ("id_no" in person and ic_shaped(value)):
            return None
        return kinds[0]
    return None


def person_value(value, said, body):
    """Whether the number the reader gave for a person, as `said`, goes on that person's
    card: a kind a person holds, at least six digits (no year, no contribution), printed
    in the document and not as an amount, and nowhere labelled as the employer's or a
    company's ("Employer EPF No: x"; an identity card's shape under "NRIC No./Company No."
    is still the card)."""
    if said not in PERSON_KEEPS or sum(c.isdigit() for c in bare(value)) < 6:
        return False
    found = spots(value, body)
    for m in found:
        label = _own_label(body, m.start())
        if EMPLOYER_LABEL.search(label) and not (said == "id_no" and ic_shaped(value) and re.search(IC_WORDS, label, re.I)):
            return False
    return bool(found)


def id_owner(value, body, said, parties, lines_above=0):
    """Whose identifier (or address) this is. The answer's party, unless the line the
    value is on names another company and not that one (a company number printed right
    after the buyer's name is the buyer's). With lines_above, the lines just before it
    count too, nearest first (a letterhead: the name, then its address)."""
    pattern = r"\s*".join(re.escape(c) for c in re.sub(r"\s+", "", value))
    m = re.search(pattern, body, re.I)
    if not m:
        return said
    start = body.rfind("\n", 0, m.start()) + 1
    end = body.find("\n", m.end())
    lines = [norm_full(body[start:(end if end != -1 else len(body))])]
    above = [l for l in body[:start].split("\n") if l.strip()][-lines_above:] if lines_above else []
    lines += [norm_full(l) for l in reversed(above)]

    def on(x, line):
        core = split_suffix(x["p"]["name"])[0] if x else ""
        return bool(core) and re.search(rf"(?<!\w){re.escape(core)}(?!\w)", line) is not None
    for line in lines:
        if said is not None and on(said, line):
            return said
        others = [x for x in parties if x is not said and x["p"]["kind"] == "company" and on(x, line)]
        if others:
            return others[0] if len(others) == 1 else said
    return said


def seed_claims(model, sysmsg, claims, rel, wiki):
    """A page that has no recorded figures yet (a person wrote it, or an older engine
    did): read its figures once, so a document that changes one of them is noticed."""
    fm, body = split_frontmatter(read(rel))
    found = LP.find_block(body)
    if found:
        body = body[:found[0]] + body[found[1]:]
    title = wiki.pages[rel]["title"]
    srcs = fm_list(fm, "sources") or []
    rec_base = {"page": rel, "source": srcs[0] if srcs else rel, "source_title": "",
                "doc_date": fm_get(fm, "updated") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", fm_get(fm, "updated") or "") else "",
                "recorded": today(), "seeded": True}
    sentinel = dict(rec_base, attribute="_seeded", value="", norm="", qualifier="", counterparty="")
    if not re.search(r"\d", body):
        claims.add(sentinel)
        return
    say("check", f"reading the figures already on {clean_line(title, 50)}'s page")
    q = doc_prefix(f"wiki page about {title}", body[:6000], 1, 1) + Q_SHORT.format(about=f" about {title}")
    try:
        a = model.ask(sysmsg, q, figures_schema([title], 20), max_tokens=1500)
    except (ModelUnavailable, DocTimeout):
        raise
    except Exception as e:  # read again next time
        log(f"figures of {rel} not read: {e}")
        return
    sc, nums = F.canon(body), F.numbers_in(body)
    figures = [f for f in a.get("figures") or [] if isinstance(f, dict)]
    if not figures:  # a short page can get an empty answer: its plainly stated terms still count
        figures = [{"about": title, "attribute": k, "qualifier": "", "value": v} for k, v, _ in F.standing_terms(body)]
    for f in figures:
        if not isinstance(f, dict) or f.get("attribute") not in F.ATTRIBUTES or f.get("about") not in (title, THIS_DOC):
            continue
        value = clean_line(f.get("value"), 120)
        q_ = without_value(clean_line(f.get("qualifier"), 100), value)
        if value and F.grounded(value, sc, nums) and F.fits(f["attribute"], value):
            claims.add(dict(rec_base, attribute=f["attribute"], qualifier="" if F.ungrounded_numbers(q_, nums) else q_,
                            counterparty="", value=value, norm=F.norm_value(f["attribute"], value)))
    claims.add(sentinel)


def body_page(wiki, body_name):
    """The existing page of a listed government body, or None: one titled with the body's
    name or one of its listed names, or carrying one as an alias the engine gave it for
    the body. Compared name for name, never by a core name: a business's page ("KWSP Sdn
    Bhd", its short alias "KWSP"; "JBPM Sdn Bhd") is never the body's."""
    for rel, pg in sorted(wiki.pages.items()):
        if not rel.startswith("wiki/companies/") or "person" in pg["tags"]:
            continue
        title = pg["title"]
        if wiki_menu.listed_exact(title) == body_name:
            return rel
        short = norm_full(split_suffix(title)[0])
        if not wiki_menu.company_named(title) and any(
                wiki_menu.listed_exact(a) == body_name and norm_full(a) != short for a in pg["aliases"]):
            return rel
    return None


def plan_document(model, wiki, claims, sysmsg, d, dest, body):
    """Pages for the parties (existing or reserved), their verified identifiers, the
    figure records, and the contradictions with what is already recorded. Asks the model
    only to read existing pages' figures once and to check pages people wrote."""
    plan = {"parties": [], "records": [], "conflicts": [], "notes": []}
    own = Run.company
    for p in d["parties"]:
        if p["kind"] not in ("company", "person", "product"):
            continue
        gov = []
        if p["kind"] == "product":
            p["name"] = clean_line(PRODUCT_NOISE.sub(" ", re.sub(r"\s*\([^)]*\)", "", p["name"])), 120) or p["name"]
            rel = wiki.find_product(p["name"])
        elif p["kind"] == "company" and wiki_menu.government_page(p["name"]):
            # A government body has one page, whatever name a document uses for it ("LHDN",
            # "Lembaga Hasil Dalam Negeri Malaysia"): titled with its name in the menu's
            # list, and carrying every other name it goes by, so each of them finds it.
            # Only a party named exactly as the body counts (wiki_menu.government_page),
            # and only a page that is the body's takes its documents (body_page): a
            # business named with an alias and more ("KWSP Sdn Bhd") keeps its own page.
            body_name = wiki_menu.government_page(p["name"])
            gov = [body_name] + wiki_menu.government_aliases(body_name)
            p["variants"] = list(dict.fromkeys(p.get("variants", []) + [p["name"]]))
            p["name"] = body_name
            rel = body_page(wiki, body_name)
        else:
            rel = wiki.find_entity(p["name"], p["kind"])
        twin = next((x for x in plan["parties"] if rel and x["rel"] == rel), None)
        if twin:  # two names in this document for one page
            twin["p"]["variants"] = list(dict.fromkeys(twin["p"].get("variants", []) + p.get("variants", [p["name"]])))
            continue
        exists = bool(rel) and os.path.exists(os.path.join(WIKI_DIR, rel))
        if not rel:
            folder = "products" if p["kind"] == "product" else "companies"
            rel = wiki.unique_path(folder, slugify(p["name"]))
            alias = clean_line(SUFFIX_ORIG_RE.sub("", p["name"]).strip(" ,."), 120) if p["kind"] == "company" else ""
            if alias and wiki_menu.listed_exact(alias):
                alias = ""   # "KWSP Sdn Bhd" is never called "KWSP": that is the EPF
            aliases = [alias] if alias and alias != p["name"] else []
            if gov:
                aliases = [a for a in dict.fromkeys(gov[1:] + p["variants"]) if norm_full(a) != norm_full(p["name"])]
            wiki.reserve(rel, label(p["name"], 120), p["kind"], [label(a, 120) for a in aliases])
        plan["parties"].append({"p": p, "rel": rel, "exists": exists, "ids": {}, "aliases": gov,
                                "own": bool(own) and p["kind"] == "company" and same_entity(own, p["name"])})
    by_name = {}
    for x in plan["parties"]:  # every spelling the parts used finds the party
        for v in x["p"].get("variants", []) + [x["p"]["name"]]:
            by_name.setdefault(norm_full(v), x)
    # Who the reader gave each number for: one given for two parties is nobody's.
    given = {}
    for i in d["identifiers"]:
        v = bare(clean_line(i.get("value"), 80)).lower()
        if v:
            x = by_name.get(norm_full(i.get("entity", "")))
            given.setdefault(v, set()).add(id(x) if x else None)
    held = set()   # a person's numbers, kept or not: never swept up for a company
    for i in d["identifiers"]:
        value = clean_line(i.get("value"), 80)
        x = by_name.get(norm_full(i.get("entity", "")))
        if not bare(value) or not x:
            continue   # nothing, or only dashes or dots ("SOCSO No: -")
        said = i.get("kind")
        if x["p"]["kind"] == "person":
            # A person's number stays with the person the reader gave it for, with the kind
            # it gave, unless the document labels it the employer's or a company's
            # (person_value). Never moved to another party, never filed as another kind.
            if person_value(value, said, body):
                held.add(bare(value).lower())
                if len(given[bare(value).lower()]) == 1:
                    x["ids"].setdefault(said, value)
            continue
        if said in PERSON_IDS:
            continue   # a company never holds a person's number, under any other kind either
        names = " ".join([x["p"]["name"]] + wiki_menu.government_aliases(x["p"]["name"]))
        kind = company_id_kind(value, body, fund=bool(re.search(r"\b(epf|kwsp|socso|perkeso)\b", names, re.I)))
        if not kind:
            continue
        x = id_owner(value, body, x, plan["parties"])
        if kind == "bank_account" and BANKISH.search(x["p"]["name"]):
            # The bank keeps the account; whose it is, the line says (or nobody can).
            holders = [y for y in plan["parties"] if y["p"]["kind"] == "company" and not BANKISH.search(y["p"]["name"])]
            x = id_owner(value, body, None, holders) if holders else None
            if not x:
                continue
        x["ids"].setdefault(kind, value)
    # A company's numbers the answers left out, found by their labels: each goes to the
    # company its line names. A number the reader gave a person is never swept up for a
    # company; one dropped as labelled the employer's or a company's ("Company No.: x"
    # given for the employee) is left for the company its line names.
    have_ids = {bare(v).lower() for x in plan["parties"] for v in x["ids"].values()} | held
    firms = [x for x in plan["parties"] if x["p"]["kind"] == "company"]
    for tok in id_tokens(body):
        if any(bare(tok).lower() in h for h in have_ids):
            continue
        kind = company_id_kind(tok, body)
        holders = [x for x in firms if not (kind == "bank_account" and BANKISH.search(x["p"]["name"]))]
        owner = id_owner(tok, body, None, holders) if holders and kind else None
        if kind and owner and kind not in owner["ids"]:
            owner["ids"][kind] = clean_line(tok, 80)

    for x in plan["parties"]:
        if x["exists"] and not wiki.pages.get(x["rel"], {}).get("engine") and not claims.seeded(x["rel"]):
            seed_claims(model, sysmsg, claims, x["rel"], wiki)

    companies = [x for x in plan["parties"] if x["p"]["kind"] == "company"]
    base = {"source": dest, "source_title": d["title"] or os.path.basename(dest), "doc_date": d["doc_date"],
            "recorded": today(), "summary": None}
    bilateral = len(companies) == 2 and companies[0]["rel"] != companies[1]["rel"]
    for f in d["figures"]:
        x = by_name.get(norm_full(f["about"]))
        if f["attribute"] not in F.STANDING:
            continue  # a one-off amount, date or reference: on the document's summary page
        if not x and not (bilateral and f["attribute"] in F.RELATIONAL):
            continue  # the document's own figure: on its summary page only
        if not x:
            x = companies[0]  # a term of an agreement between two companies holds for both
        if identifier_kind(f["value"], body):
            continue  # a registration, tax or account number: the Info card holds it
        if f["attribute"] == "address" and x["p"]["kind"] == "company":
            x = id_owner(f["value"], body, x, plan["parties"], lines_above=2)
        rec = dict(base, page=x["rel"], attribute=f["attribute"], qualifier=f["qualifier"], counterparty="",
                   value=f["value"], norm=F.norm_value(f["attribute"], f["value"]))
        recs = [rec]
        if f["attribute"] in F.RELATIONAL and bilateral and x in companies:
            other = companies[1] if x is companies[0] else companies[0]
            rec["counterparty"] = other["rel"]
            recs.append(dict(rec, page=other["rel"], counterparty=x["rel"], mirror=True))
        flagged = False
        for r in recs:
            old = claims.conflict(r)
            if old:
                if not flagged:  # the same term on both parties' pages is one question
                    plan["conflicts"].append((old, r))
                flagged = True
                r["conflicted"] = True
            plan["records"].append(r)

    # Pages people wrote can hold facts that are not figures; the model compares those.
    for x in plan["parties"]:
        if not x["exists"] or wiki.pages.get(x["rel"], {}).get("engine"):
            continue
        nm = x["p"]["name"]
        stmts = [f"{nm}: {x['p']['role']}"] if x["p"]["role"] else []
        stmts += [s for s in d["summary"] if norm_full(nm.split()[0]) in norm_full(s) and not re.search(r"\d", s)]
        if not stmts:
            continue
        existing = split_frontmatter(read(x["rel"]))[1]
        found = LP.find_block(existing)
        if found:
            existing = existing[:found[0]] + existing[found[1]:]
        say("check", f"checking {clean_line(nm, 60)} against its page")
        c = ask_safe(model, sysmsg, f"Existing wiki page about {nm}:\n<page>\n{existing[:6000]}\n</page>\n\n"
                     f"New statements from {os.path.basename(dest)}:\n" + "\n".join(f"- {s}" for s in stmts) + "\n\n"
                     "Compare them one by one. A contradiction is the page and a new statement giving different "
                     "values for the same thing: a different person in a role, a different address, a different "
                     "status. Statements that only add new information are not contradictions; return an empty "
                     "list if there are none.", CONFLICT_SCHEMA, 700, f"check of {x['rel']}")
        for k in c.get("conflicts") or []:
            plan["notes"].append((x, f"{nm}: the page says \"{clean_line(k.get('existing'), 160)}\"; "
                                     f"{os.path.basename(dest)} says \"{clean_line(k.get('new'), 160)}\""))
    return plan


def write_document(wiki, claims, src, dest, project, d, plan, truncated, n_parts, touched):
    title = d["title"] or os.path.splitext(os.path.basename(src))[0]
    slug = slugify(os.path.splitext(os.path.basename(dest))[0])
    summary_rel = wiki.unique_path("sources", slug, source=dest)
    created = summary_rel not in wiki.pages
    wiki.pages.setdefault(summary_rel, {"title": label(title, 160), "aliases": [], "tags": [d["doc_type"]], "engine": True})
    rel_of = {norm_full(v): x["rel"] for x in plan["parties"] for v in x["p"].get("variants", []) + [x["p"]["name"]]}

    # Topic pages and decision pages this document creates.
    for t in d["topics"]:
        if not t["rel"]:
            t["rel"] = wiki.unique_path(t["section"], slugify(t["title"]))
            wiki.reserve(t["rel"], label(t["title"], 80), "topic")
    decisions = []
    for dc in d["decisions"]:
        date = dc["date"] or d["doc_date"] or today()
        rel = wiki.unique_path("decisions", f"{date}-{slugify(dc['title'], 50)}", source=dest)
        while rel in [x["rel"] for x in decisions]:
            rel = rel[:-3] + "-2.md" if not re.search(r"-\d+\.md$", rel) else re.sub(
                r"-(\d+)\.md$", lambda m: f"-{int(m.group(1)) + 1}.md", rel)
        wiki.reserve(rel, label(dc["title"], 90), "decision")
        decisions.append(dict(dc, rel=rel, date=date))

    # Records: figures, the parties' roles, relationships, topic points.
    for r in plan["records"]:
        r["summary"] = summary_rel
        r["source_title"] = label(title, 120)
        claims.add(r)
    base = {"source": dest, "source_title": label(title, 120), "doc_date": d["doc_date"], "recorded": today(),
            "summary": summary_rel, "qualifier": "", "counterparty": "", "norm": ""}
    for x in plan["parties"]:
        claims.add(dict(base, page=x["rel"], attribute="_doc", value=x["p"]["role"]))
    rels = []
    for r in d["relations"]:
        a, b = rel_of.get(norm_full(r["subject"])), rel_of.get(norm_full(r["object"]))
        relation = r["relation"]
        if relation in ("customer_of", "tenant_of"):  # one way of saying each: the supplier's, the landlord's
            relation, a, b = {"customer_of": "supplier_of", "tenant_of": "landlord_of"}[relation], b, a
        if relation == "bank_of" and a and b and not BANKISH.search(r["subject"]) and BANKISH.search(r["object"]):
            a, b = b, a
        if a and b and a != b and (a, relation, b) not in rels:
            rels.append((a, relation, b))
            claims.add(dict(base, page=a, attribute="_rel", qualifier=relation, counterparty=b, value=""))
    for t in d["topics"]:
        for pt in t["points"]:
            claims.add(dict(base, page=t["rel"], attribute="_point", value=pt))
        if not t["points"]:
            claims.add(dict(base, page=t["rel"], attribute="_point", value=d["summary"][0] if d["summary"] else title))
    claims.save()

    # The summary page first, so every link to it resolves even if a later step fails.
    d["menu_paths"] = doc_menu(d, title)
    write(summary_rel, summary_page(wiki, d, dest, project, title, plan, rels, decisions, truncated, n_parts))
    page_event(summary_rel, created, wiki)

    conflicted = {r["page"] for r in plan["records"] if r.get("conflicted")} | {x["rel"] for x, _ in plan["notes"]}
    for x in plan["parties"]:
        write_entity(wiki, claims, x, dest, summary_rel, title, x["rel"] in conflicted)
        touched.setdefault(x["rel"], "entity")
    for t in d["topics"]:
        write_topic(wiki, claims, t["rel"], t["title"], dest, d["menu_paths"])
        touched.setdefault(t["rel"], "topic")
    for dc in decisions:
        write_decision(wiki, dc, dest, summary_rel, title, plan)

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
    for x in plan["parties"]:
        index_add(wiki, x["rel"], "Products" if x["p"]["kind"] == "product" else "Companies and people",
                  x["p"]["role"] or x["p"]["kind"])
    for t in d["topics"]:
        index_add(wiki, t["rel"], SECTIONS[t["rel"].split("/")[1]], (t["points"] or [t["title"]])[0])
    for dc in decisions:
        index_add(wiki, dc["rel"], "Decisions", dc["decision"])

    for old, new in plan["conflicts"]:
        what = F.label_of(new["attribute"]).lower() + (f" ({new['qualifier']})" if new.get("qualifier") else "")
        page_title = wiki.pages.get(new["page"], {}).get("title") or new["page"]
        dated = lambda r: f"{source_name(r)}, {r['doc_date']}" if r.get("doc_date") else f"{source_name(r)}, undated"  # noqa: E731
        if F.Claims.order(new) >= F.Claims.order(old):
            outcome = f"The page now shows {new['value']}, from the later document, with {old['value']} as the previous value."
        else:
            outcome = (f"The page still shows {old['value']}: {source_name(new)} is "
                       + ("older." if new.get("doc_date") else "undated."))
        lines = [f"{page_title} — {what}: {old['value']} ({dated(old)}) against {new['value']} ({dated(new)}).",
                 outcome + " Confirm which is right."]
        if Run.reader == "claude":
            lines.append(f"Review item: `{conflict_item(project, new, old, page_title, what, dated)}`")
        review(project, new["page"], "a new document disagrees with an existing page", lines)
    flagged = {r["page"] for _, r in plan["conflicts"]}
    for x, line in plan["notes"]:
        if x["rel"] not in flagged:  # a figure already flagged on this page says enough
            review(project, x["rel"], "a new document disagrees with an existing page", [line])
    if truncated:
        review(project, summary_rel, "long document only partly read",
               [f"{os.path.basename(dest)} is long; only its start was read." if Run.reader == "claude"
                else f"{os.path.basename(dest)} has more than {MAX_PARTS} parts; only the first were read."])


def conflict_item(project, new, old, page_title, what, dated):
    """The contradiction as a question in the Upload page's Review tab, which the owner (or
    Jev, in Auto-review) answers with one click. Returns its id."""
    newer = F.Claims.order(new) >= F.Claims.order(old)
    context = clean_line(f"{source_name(old)} ({old.get('source')}, {dated(old)}) says {old['value']}; "
                         f"{source_name(new)} ({new.get('source')}, {dated(new)}) says {new['value']}.", 1200)
    for folder in (wiki_review.OPEN, wiki_review.DONE):   # the same question again (a retried document)
        try:
            names = [n for n in os.listdir(folder) if n.endswith(".json")]
        except OSError:
            continue
        for n in names:
            try:
                with open(os.path.join(folder, n), encoding="utf-8") as f:
                    if json.load(f).get("context") == context:
                        return n[:-5]
            except (OSError, ValueError, AttributeError):
                continue
    iid = wiki_review.new_id(f"{page_title} {what}")
    item = {
        "id": iid, "created": today(), "kind": "contradiction", "file": new.get("source") or "",
        "question": clean_line(f"{page_title}: which {what} is right now?", 400),
        "context": context,
        "pages": [new["page"]],
        "options": [
            {"key": "A", "label": clean_line(new["value"], 200),
             "effect": clean_line(f"The page shows {new['value']} as the current {what}, from {source_name(new)}; "
                                  f"{old['value']} stays as the previous value.", 400)},
            {"key": "B", "label": clean_line(old["value"], 200),
             "effect": clean_line(f"The page keeps {old['value']} as the current {what}; {source_name(new)} is "
                                  "noted as disagreeing.", 400)},
            {"key": "C", "label": "Both are right: they apply to different things", "auto": False,
             "effect": "Both values stay on the page, each with what it applies to."},
        ],
        "recommended": "A" if newer else "B",
    }
    try:
        wiki_review.write_json(os.path.join(wiki_review.OPEN, iid + ".json"), item)
        wiki_events.emit("review-item", id=iid, question=item["question"], file=new.get("source") or "")
    except OSError as e:
        log(f"review item for {page_title} not written: {e}")
    return iid


BANKISH = re.compile(r"\bbank\b|\bbanking\b|maybank|\bcimb\b|\brhb\b|ambank|\bocbc\b|\bhsbc\b|\buob\b|\baffin\b|\bbsn\b", re.I)


def source_name(r):
    return r.get("source_title") or os.path.basename(r.get("source") or "") or "a page"


def link_of(wiki):
    def f(rel, text=None):
        return wiki.link(rel, text) if rel in wiki.pages else esc(text or rel)
    return f


def menu_line(paths):
    """A page's place in the wiki's menu, as a frontmatter line (scripts/wiki_menu.py)."""
    return "menu: [" + ", ".join(yq(p) for p in paths) + "]"


def topic_menu(rel, title):
    """A topic page's place in the menu: its own menu: when it has one, else where its
    title says (a starter topic's place, or its words')."""
    if rel and os.path.exists(os.path.join(WIKI_DIR, rel)):
        own = wiki_menu.normalise(fm_list(split_frontmatter(read(rel))[0], "menu") or [])
        if own:
            return own
    return wiki_menu.topic_paths(title)


def doc_menu(d, title):
    """Where a document's summary page sits in the menu: where the reader put it; else
    where the topic pages it adds to are (two at most); else where its title and type say
    (one); else nowhere, and Claude may place it later (wiki_menu.py ask)."""
    menu = wiki_menu.normalise(d.get("menu"))[:2]
    if menu:
        return menu
    for t in d["topics"]:
        menu += [p for p in topic_menu(t["rel"], t["title"]) if p not in menu]
    if menu:
        return menu[:2]
    return wiki_menu.keyword_paths(f"{title} {d['doc_type']}", doc=True)[:1]


def summary_page(wiki, d, dest, project, title, plan, rels, decisions, truncated, n_parts):
    link = link_of(wiki)
    lines = ["---", f"title: {yq(label(title, 160))}", "type: summary", f"tags: [{yq(d['doc_type'])}]",
             f"sources: [{yq(dest)}]",
             "status: needs-review" if (truncated or plan["conflicts"] or plan["notes"]) else "status: draft",
             f"updated: {today()}", f"project: {yq(project)}"]
    if d["doc_date"]:
        lines.append(f"doc_date: {yq(d['doc_date'])}")
    if d.get("menu_paths"):
        lines.append(menu_line(d["menu_paths"]))
    lines += ["engine: local", "---", ""]
    who = "from Claude's reading" if Run.reader == "claude" else "by the model on this Mac"
    lines.append(f"> Summary of {raw_link(dest)} written {who}. Every figure on this page "
                 "was found in the document; check the original before relying on one.")
    lines.append("")
    if d["summary"]:
        lines += ["## Summary", ""] + [f"- {esc(s)}" for s in d["summary"]] + [""]
    if d.get("photo"):
        lines += ["## What the photo shows", "", esc(d["photo"], 1500), ""]
        if d.get("taken"):
            lines += [f"Taken {esc(d['taken'], 20)}.", ""]
    rel_of = {norm_full(v): x["rel"] for x in plan["parties"] for v in x["p"].get("variants", []) + [x["p"]["name"]]}
    if d["figures"]:
        lines += ["## Key facts", "", "| Fact | About | Value |", "|---|---|---|"]
        for f in d["figures"][:60]:
            about = rel_of.get(norm_full(f["about"]))
            fact = F.label_of(f["attribute"]) + (f" ({f['qualifier']})" if f["qualifier"] else "")
            lines.append(f"| {esc(fact, 160)} | {LP.cell(link(about)) if about else '—'} | {esc(f['value'], 160)} |")
        lines.append("")
    ids = [(x, k, v) for x in plan["parties"] for k, v in x["ids"].items()]
    if ids:
        lines += ["## Identifiers", ""] + [f"- {link(x['rel'])}: {k.replace('_', ' ')} {esc(v, 80)}" for x, k, v in ids] + [""]
    if plan["parties"] or any(p["kind"] in ("place", "other") for p in d["parties"]):
        lines += ["## Parties", ""]
        lines += [f"- {link(x['rel'])}" + (f" — {esc(x['p']['role'], 160)}" if x["p"]["role"] else "") for x in plan["parties"]]
        lines += [f"- {esc(p['name'], 120)} ({p['kind']})" + (f" — {esc(p['role'], 160)}" if p["role"] else "")
                  for p in d["parties"] if p["kind"] in ("place", "other")]
        lines.append("")
    if rels:
        lines += ["## Relationships", ""]
        lines += [f"- {LP.relation_line(link(a), r, link(b))}" for a, r, b in rels]
        lines.append("")
    if d["obligations"]:
        lines += ["## Obligations and deadlines", ""] + [f"- {esc(x)}" for x in d["obligations"]] + [""]
    if decisions:
        lines += ["## Decisions", ""] + [f"- {link(dc['rel'])} — {esc(dc['decision'])}" for dc in decisions] + [""]
    if d["open_questions"]:
        lines += ["## Open questions", ""] + [f"- {esc(x)}" for x in d["open_questions"]] + [""]
    if d["topics"]:
        lines += ["## Topics", ""] + [f"- {link(t['rel'])}" for t in d["topics"]] + [""]
    if truncated:
        lines += ["_Only the start of this long document was read._" if Run.reader == "claude"
                  else f"_Only the first {n_parts} parts of this long document were read._", ""]
    return "\n".join(lines)


def entity_overview_fallback(wiki, rel, claims):
    """A first overview from what is recorded, until the model writes one."""
    p = wiki.pages.get(rel, {})
    docs = claims.records("_doc", rel)
    roles = list(dict.fromkeys(r["value"] for r in docs if r.get("value")))[:3]
    kind = next((t for t in p.get("tags", []) if t in ("company", "person", "product")), "")
    text_ = f"{p.get('title', '')}" + (f" ({kind})" if kind else "")
    if roles:
        text_ += ": " + "; ".join(roles)
    text_ += f". Named in {len({r.get('source') for r in docs})} document(s) so far; the facts and documents are below."
    return esc(text_, 600)


def entity_block(wiki, claims, rel, overview=None):
    link = link_of(wiki)
    title_of = lambda r: wiki.pages.get(r, {}).get("title") or r  # noqa: E731
    body = split_frontmatter(read(rel))[1] if os.path.exists(os.path.join(WIKI_DIR, rel)) else ""
    ov = overview or LP.block_section(body, "Overview") or entity_overview_fallback(wiki, rel, claims)
    return LP.entity_block(ov, LP.facts_table(claims.facts(rel), title_of, link),
                           LP.related_lines(rel, claims.records("_rel", rel, other=True), link),
                           LP.documents_lines(claims.records("_doc", rel), link))


def write_entity(wiki, claims, x, dest, summary_rel, summary_title, conflicted):
    e, rel = x["p"], x["rel"]
    path = os.path.join(WIKI_DIR, rel)
    if not os.path.exists(path):
        tags = [e["kind"]] + (["own-company"] if x.get("own") else [])
        lines = ["---", f"title: {yq(label(e['name'], 120))}", "type: entity",
                 f"tags: [{', '.join(yq(t) for t in tags)}]", f"sources: [{yq(dest)}]",
                 "status: needs-review" if conflicted else "status: draft", f"updated: {today()}"]
        aliases = wiki.pages.get(rel, {}).get("aliases") or []
        if aliases:
            lines.append(f"aliases: [{', '.join(yq(a) for a in aliases)}]")
        if x["ids"]:
            lines.append("facts:")
            lines += [f"  {k}: {yq(v)}" for k, v in x["ids"].items()]
        lines += ["engine: local", "---", "", LP.render_block(entity_block(wiki, claims, rel)), ""]
        write(rel, "\n".join(lines))
        page_event(rel, True, wiki)
        return
    fm, body = split_frontmatter(read(rel))
    new_body, outcome = LP.put_block(body, entity_block(wiki, claims, rel), "top")
    if outcome == "edited":
        # A person edited the engine's block: it stays as they left it, and this document
        # is noted below it instead.
        if not wiki.has_link(rel, summary_rel):
            append_under(rel, "From sources", [f"- {today()} — {esc(e['role'] or 'mentioned', 160)} "
                                               f"({wiki.link(summary_rel, summary_title)})"])
            fm, new_body = split_frontmatter(read(rel))
        review("general", rel, "the engine's section was edited by hand", [
            f"{wiki.pages[rel]['title']}: its overview and facts were edited by hand, so the wiki no longer updates "
            "them. Remove the two wiki-engine marker lines to let it update them again."])
    if fm:
        fm = merge_ids(fm, x["ids"], rel, wiki, dest)
        fm = merge_aliases(fm, x.get("aliases"), wiki.pages[rel]["title"])
        write(rel, "---\n" + fm.strip("\n") + "\n---\n" + new_body)
    else:
        write(rel, new_body)
    touch_page(rel, add_source=dest, status="needs-review" if conflicted else None)
    page_event(rel, False, wiki)


def merge_ids(fm, ids, rel, wiki, dest):
    """Verified identifiers added to a page's facts: map (its Info card). A key the card
    already has is never changed; a different value goes to the review queue ("an
    identifier differs"). That is the net under trusting the reader with a person's
    numbers (person_value): a slip never replaces a number, and a person sees it. A facts:
    entry in a shape this does not write (a flow map, a comment) is left alone."""
    if not ids:
        return fm
    blk = _key_block(fm, "facts")
    if not blk:
        return fm.rstrip("\n") + "\nfacts:\n" + "".join(f"  {k}: {yq(v)}\n" for k, v in ids.items())
    start, end, first = blk
    if re.sub(r"\s*#.*$", "", first) not in ("", "{}"):
        log(f"{rel}: its facts: entry is not a plain list of keys; identifiers left out")
        return fm
    lines = [l for l in fm[start:end].split("\n")[1:] if l.strip()]
    have = {}
    for line in lines:
        m = re.match(r"^\s+([\w-]+):\s*(.*)$", line)
        if m:
            have[m.group(1)] = fm_get("v: " + m.group(2), "v")
    for k, v in ids.items():
        if k in have and re.sub(r"\s+", "", have[k]).lower() != re.sub(r"\s+", "", v).lower():
            review("general", rel, "an identifier differs", [
                f"{wiki.pages[rel]['title']}: the Info card says {k.replace('_', ' ')} {have[k]}; "
                f"{os.path.basename(dest)} says {v}. The card was left as it is."])
    add = [f"  {k}: {yq(v)}" for k, v in ids.items() if k not in have]
    if not add:
        return fm
    return fm[:start] + "\n".join(["facts:"] + lines + add) + fm[end:]


def merge_aliases(fm, aliases, title):
    """Other names of a page (a government body's) added to its aliases: none is ever
    taken out, and an aliases: entry in a shape this does not write is left alone."""
    have = fm_list(fm, "aliases") if aliases else None
    if have is None:
        return fm
    known = {norm_full(title)} | {norm_full(h) for h in have}
    add = [label(a, 120) for a in dict.fromkeys(aliases) if norm_full(a) not in known]
    if not add:
        return fm
    return fm_set(fm, "aliases", "aliases: [" + ", ".join(yq(a) for a in have + add) + "]")


def topic_block(wiki, claims, rel, overview=None):
    engine_page = wiki.pages.get(rel, {}).get("engine", True)
    body = split_frontmatter(read(rel))[1] if os.path.exists(os.path.join(WIKI_DIR, rel)) else ""
    points = claims.records("_point", rel)
    fallback = esc("; ".join(dict.fromkeys(r["value"] for r in points))[:400] or wiki.pages.get(rel, {}).get("title"), 600)
    ov = overview or LP.block_section(body, "Overview") or fallback
    return LP.topic_block(ov, points, link_of(wiki), with_overview=engine_page)


def write_topic(wiki, claims, rel, title, dest, doc_paths=()):
    path = os.path.join(WIKI_DIR, rel)
    if not os.path.exists(path):
        # Its place in the menu: a starter topic's, or the one its title names, or else the
        # document's that created it.
        paths = wiki_menu.topic_paths(title) or list(doc_paths)[:1]
        lines = ["---", f"title: {yq(label(title, 80))}", "type: concept", "tags: [topic]",
                 f"sources: [{yq(dest)}]", "status: draft", f"updated: {today()}"]
        lines += ([menu_line(paths)] if paths else []) + ["engine: local", "---", "",
                                                          LP.render_block(topic_block(wiki, claims, rel)), ""]
        write(rel, "\n".join(lines))
        page_event(rel, True, wiki)
        return
    fm, body = split_frontmatter(read(rel))
    engine_page = wiki.pages.get(rel, {}).get("engine")
    new_body, outcome = LP.put_block(body, topic_block(wiki, claims, rel), "top" if engine_page else "end")
    if outcome == "written":
        write(rel, ("---\n" + fm.strip("\n") + "\n---\n" if fm else "") + new_body)
    touch_page(rel, add_source=dest)
    page_event(rel, False, wiki)


def write_decision(wiki, dc, dest, summary_rel, summary_title, plan):
    rel = dc["rel"]
    created = not os.path.exists(os.path.join(WIKI_DIR, rel))
    names = [(x["p"]["name"], wiki.link(x["rel"])) for x in plan["parties"]]
    content = "\n".join([f"**Decided {dc['date']}:** {LP.linked(dc['decision'], names)}", "",
                         f"Recorded in {wiki.link(summary_rel, summary_title)}.", ""])
    if created:
        write(rel, "\n".join(["---", f"title: {yq(label(dc['title'], 90))}", "type: decision", "tags: [decision]",
                              f"sources: [{yq(dest)}]", "status: active", f"updated: {today()}",
                              f"decided: {yq(dc['date'])}", "engine: local", "---", "", LP.render_block(content), ""]))
    else:
        fm, body = split_frontmatter(read(rel))
        new_body, outcome = LP.put_block(body, content, "top")
        if outcome != "written":
            return
        write(rel, ("---\n" + fm.strip("\n") + "\n---\n" if fm else "") + new_body)
    page_event(rel, created, wiki)


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


def process_packet(model, wiki, claims, src, sysmsg, touched):
    Run.current = src
    wiki_events.emit("read", file=src)
    log(f"applying {src}")
    say("apply", "reading the update")
    text_ = read(src)
    if "<<<<<<<" in text_ and ">>>>>>>" in text_:
        text_ = re.sub(r"^(<<<<<<<|=======|>>>>>>>).*$", "", text_, flags=re.M)
    packets = parse_packets(text_) or ([note_as_packet(text_, os.path.basename(src))] if text_.strip() else [])
    archived = choose_dest("archive/inbox", os.path.basename(src), src)
    model.deadline = time.time() + Run.doc_budget
    try:
        planned = [(p, plan_packet(model, wiki, claims, p, sysmsg, archived)) for p in packets]
    finally:
        model.deadline = None
    say("write", "writing the update into the wiki")
    for p, plan in planned:
        apply_packet(wiki, claims, p, archived, plan, touched)
    say("file", "archiving it")
    place(src, archived)
    wiki_events.emit("applied", file=src, to=archived)
    return True


def packet_text(p):
    return "\n".join([p["title"], p["Summary"]] + p["Details"] + [p["Supersedes"]])


def plan_packet(model, wiki, claims, p, sysmsg, archived):
    """The existing pages an update names: matched by code, and by the model only for the
    names code cannot match, choosing from candidates it is shown. A name with a section
    ('how-it-runs/stock-count') that has no page yet becomes a new topic page. Then the
    update's figures, for the claims of the pages it names."""
    targets, unresolved, create = [], [], []
    for t in p["Affects pages"]:
        r = wiki.resolve(t)
        if r:
            targets.append(r)
            continue
        m = re.match(r"^\s*(?:wiki/)?(finance-legal|how-it-runs)/([\w-]+?)(?:\.md)?\s*$", t.strip().strip("`*[]"))
        if m:
            create.append((f"wiki/{m.group(1)}/{slugify(m.group(2))}.md", m.group(2).replace("-", " ").capitalize()))
        else:
            unresolved.append(clean_line(t, 120))
    if unresolved:
        words = set(norm_full(" ".join(unresolved) + " " + p["title"]).split())
        cands = sorted((x for x in wiki.pages if not x.startswith(("wiki/sources/", "wiki/updates/"))),
                       key=lambda x: (-len(words & set(norm_full(x.replace("/", " ").replace("-", " ")
                                                                  + " " + wiki.pages[x]["title"]).split())), x))
        cands = cands[:MAX_PAGE_CHOICES]
        if cands:
            say("ask", "matching the pages it names")
            listing = "\n".join(f"- {c} ({clean_line(wiki.pages[c]['title'], 80)})" for c in cands)
            ans = ask_safe(model, sysmsg, "An update says it affects these pages: " + "; ".join(unresolved)
                           + f"\nUpdate: {p['title']}. {clean_line(p['Summary'], 400)}\n\n"
                           "Existing pages:\n" + listing + "\n\nWhich of the existing pages are meant? "
                           "Choose only clear matches; return an empty list if none fits.",
                           obj({"pages": S("array", items=S("string", enum=cands), maxItems=8)}), 400,
                           "page matching")
            targets += ans.get("pages") or []
    targets = [t for i, t in enumerate(targets) if t in wiki.pages and t not in targets[:i]]
    create = [c for i, c in enumerate(create) if c[0] not in wiki.pages and c not in create[:i]]

    # The update's figures, for the pages it names that hold claims (a company, a person,
    # a product or a topic).
    records, conflicts = [], []
    body = packet_text(p)
    pages = [t for t in targets if t.startswith(("wiki/companies/", "wiki/products/", "wiki/finance-legal/",
                                                 "wiki/how-it-runs/"))]
    if pages and re.search(r"\d", body):
        names = [wiki.pages[t]["title"] for t in pages]
        for t in pages:
            if not claims.seeded(t) and not wiki.pages[t].get("engine"):
                seed_claims(model, sysmsg, claims, t, wiki)
        say("ask", "finding the figures it changes")
        q = ("This is an update the business owner wrote.\n<document>\n" + body[:8000] + "\n</document>\n\n"
             + Q_SHORT.format(about=" about " + "; ".join(names)))
        a = ask_safe(model, sysmsg, q, figures_schema(names, 15), 1500, "figures of the update")
        sc, nums = F.canon(body), F.numbers_in(body)
        by_title = {norm_full(wiki.pages[t]["title"]): t for t in pages}
        for f in a.get("figures") or []:
            if not isinstance(f, dict) or f.get("attribute") not in F.ATTRIBUTES:
                continue
            page = by_title.get(norm_full(f.get("about")))
            value = clean_line(f.get("value"), 120)
            if not page or not value or not F.grounded(value, sc, nums) or not F.fits(f["attribute"], value):
                continue
            q_ = clean_line(f.get("qualifier"), 100)
            rec = {"page": page, "attribute": f["attribute"], "qualifier": "" if F.ungrounded_numbers(q_, nums) else q_,
                   "counterparty": "", "value": value, "norm": F.norm_value(f["attribute"], value),
                   "source": archived, "source_title": label(p["title"], 120), "doc_date": p["date"],
                   "recorded": today(), "summary": None, "supersedes": not is_empty(p["Supersedes"])}
            old = claims.conflict(rec)
            if old:
                conflicts.append((old, rec))
            records.append(rec)
    return {"targets": targets, "unresolved": unresolved, "create": create, "records": records,
            "conflicts": conflicts}


def apply_packet(wiki, claims, p, archived, plan, touched):
    targets, unresolved = plan["targets"], plan["unresolved"]
    kind = (str(p["Type"]).split("|")[0].strip().lower() or "fact")
    folder = "decisions" if kind == "decision" else "updates"
    slug = f"{p['date']}-{slugify(p['title'], 50)}"
    rel = wiki.unique_path(folder, slug, source=archived)
    created = rel not in wiki.pages
    targets = [t for t in targets if t != rel]
    wiki.reserve(rel, label(p["title"], 160), kind)
    for t_rel, t_title in plan["create"]:
        wiki.reserve(t_rel, label(t_title, 80), "topic")
    new_topics = [t for t, _ in plan["create"]]

    # Records: the figures (on their pages), the update on every page it names.
    for r in plan["records"]:
        r["summary"] = rel
        claims.add(r)
    base = {"source": archived, "source_title": label(p["title"], 120), "doc_date": p["date"], "recorded": today(),
            "summary": rel, "qualifier": "", "counterparty": "", "norm": ""}
    point = clean_line(p["Summary"] or p["title"], 300)
    for t in targets + new_topics:
        if t.startswith(("wiki/finance-legal/", "wiki/how-it-runs/")):
            claims.add(dict(base, page=t, attribute="_point", value=point))
            for x in p["Details"][:6]:
                claims.add(dict(base, page=t, attribute="_point", value=clean_line(x, 300)))
        elif t.startswith(("wiki/companies/", "wiki/products/")):
            claims.add(dict(base, page=t, attribute="_doc", value=""))
    claims.save()

    superseded = [(o, r) for o, r in plan["conflicts"] if r.get("supersedes")]
    lines = ["---", f"title: {yq(label(p['title'], 160))}", f"type: {'decision' if folder == 'decisions' else 'summary'}",
             f"tags: [{yq(slugify(kind, 30))}]", f"sources: [{yq(archived)}]",
             "status: needs-review" if (not is_empty(p["Supersedes"]) and not superseded) else "status: active",
             f"updated: {today()}", f"project: {yq(p['project'])}", "engine: local", "---", ""]
    if p["Summary"]:
        lines += [owner_text(p["Summary"]), ""]
    if p["Details"]:
        lines += ["## Details", ""] + [f"- {owner_text(x)}" for x in p["Details"]] + [""]
    if targets or new_topics:
        lines += ["## Affects", ""] + [f"- {wiki.link(t)}" for t in targets + new_topics] + [""]
    if not is_empty(p["Supersedes"]):
        lines += ["## Supersedes", "", owner_text(p["Supersedes"]), ""]
        lines += [f"- {esc(wiki.pages.get(r['page'], {}).get('title', r['page']), 120)}: "
                  f"{esc(F.label_of(r['attribute']).lower())} {esc(o['value'], 80)} → {esc(r['value'], 80)}"
                  for o, r in superseded] + ([""] if superseded else [])
    if not is_empty(p["Open questions"]):
        lines += ["## Open questions", "", owner_text(p["Open questions"]), ""]
    lines += [f"_From the Update Packet {raw_link(archived)}, dated {p['date']}._", ""]
    write(rel, "\n".join(lines))
    page_event(rel, created, wiki)

    for t_rel, t_title in plan["create"]:
        write_topic(wiki, claims, t_rel, t_title, archived)
        touched.setdefault(t_rel, "topic")
        index_add(wiki, t_rel, SECTIONS[t_rel.split("/")[1]], point)
    entry = f"- **{p['date']}** — {esc(p['Summary'] or p['title'], 300)} ({wiki.link(rel, 'details')})"
    for t in targets:
        if t.startswith(("wiki/companies/", "wiki/products/")):
            touched.setdefault(t, "entity")
        elif t.startswith(("wiki/finance-legal/", "wiki/how-it-runs/")):
            write_topic(wiki, claims, t, wiki.pages[t]["title"], archived)
            touched.setdefault(t, "topic")
        if wiki.has_link(t, rel):
            continue
        append_under(t, "Updates", [entry])
        touch_page(t, add_source=archived, status="needs-review" if any(
            r["page"] == t and not r.get("supersedes") for _, r in plan["conflicts"]) else None)
        page_event(t, False, wiki)

    index_add(wiki, rel, "Decisions" if folder == "decisions" else "Updates", p["Summary"] or p["title"])
    log_line("ingest", p["project"], p["title"])
    for o, r in plan["conflicts"]:
        if r.get("supersedes"):
            continue
        page_title = wiki.pages.get(r["page"], {}).get("title") or r["page"]
        review(p["project"], r["page"], "an update disagrees with an existing page", [
            f"{page_title} — {F.label_of(r['attribute']).lower()}: {o['value']} ({source_name(o)}) against "
            f"{r['value']} ({p['title']}, {p['date']}).",
            "The update does not say it supersedes the earlier value. Confirm which is right."])
    if not is_empty(p["Supersedes"]) and not superseded:
        review(p["project"], rel, "an update supersedes an earlier claim", [
            f"{p['title']}: supersedes \"{clean_line(p['Supersedes'], 240)}\".",
            "Find the old claim on the affected pages and mark it superseded."])
    if not is_empty(p["Open questions"]):
        review(p["project"], rel, "open questions from an update", [clean_line(p["Open questions"], 400)])
    if unresolved and not targets and not new_topics:
        review(p["project"], rel, "affected pages not found",
               [f"{p['title']} names pages that do not exist yet: {', '.join(unresolved)}."])


# ================================================================ end of run =====
def prose_context(wiki, claims, rel, kind, roles=True):
    """What the model may use for a page's overview: its recorded facts, relationships
    and roles (an entity), or the points its documents make (a topic). roles=False leaves
    the roles out (to tell whether only roles changed since the last overview)."""
    title = wiki.pages[rel]["title"]
    title_of = lambda r: wiki.pages.get(r, {}).get("title") or r  # noqa: E731
    if kind == "topic":
        pts = claims.records("_point", rel)
        if not pts:
            return ""
        return f"Topic: {title}\nPoints from its documents:\n" + "\n".join(
            f"- {r['value']} ({r.get('source_title') or 'a document'}, {r.get('doc_date') or 'undated'})"
            for r in sorted(pts, key=F.Claims.order, reverse=True)[:30])
    tags = wiki.pages[rel].get("tags") or []
    kind_word = next((t for t in tags if t in ("company", "person", "product")), "")
    lines = [f"Page: {title}" + (f" ({kind_word})" if kind_word else "")]
    own = "own-company" in tags
    if own:
        lines.append("This is the business's own company: the documents are its records.")
    if roles and not own:
        roles = [f"{r['value']} ({r.get('source_title') or 'a document'})" for r in claims.records("_doc", rel) if r.get("value")]
    else:
        roles = []
    if roles:
        lines += ["Its role in the documents:"] + [f"- {x}" for x in list(dict.fromkeys(roles))[:12]]
    facts = []
    # A person's page gets no dates or references: "prepared on 3 July" is about a document.
    skip = {"date", "deadline", "reference", "other"} | ({"address", "start_date", "end_date", "due_date"}
                                                        if kind_word == "person" else set())
    groups = [g for g in claims.facts(rel) if g[0]["attribute"] not in skip]
    groups = [g for g in groups if g[0]["attribute"] in F.COMPARED] + [g for g in groups if g[0]["attribute"] not in F.COMPARED]
    for g in groups[:15]:
        cur = g[0]
        older = [r for r in g[1:] if not F.same_norm(r.get("norm"), cur.get("norm"))]
        facts.append(f"- {LP.fact_label(cur, title_of)}: {cur['value']}"
                     + (f" (before: {older[0]['value']})" if older else ""))
    if facts:
        lines += ["Current facts:"] + facts
    rels = []
    for r in LP.plausible(claims.records("_rel", rel, other=True)):
        rels.append("- " + LP.relation_sentence(title_of(r["page"]), r.get("qualifier"), title_of(r["counterparty"])))
    if rels:
        lines += ["Relationships:"] + list(dict.fromkeys(rels))[:15]
    body = split_frontmatter(read(rel))[1]
    found = LP.find_block(body)
    human = (body[:found[0]] + body[found[1]:]) if found else body
    human = re.sub(r"^## (From sources|Updates)\s*\n(?:.*\n?)*?(?=^## |\Z)", "", human, flags=re.M).strip()
    if human:
        lines += ["What the page already says:", human[:1200]]
    return "\n".join(lines) if len(lines) > 1 else ""


def link_names(wiki, rel):
    names = []
    for r, p in wiki.pages.items():
        if r == rel or not r.startswith(("wiki/companies/", "wiki/products/", "wiki/finance-legal/", "wiki/how-it-runs/")):
            continue
        if os.path.basename(r)[:-3] in SEED_PAGES:
            continue
        names.append((p["title"], wiki.link(r)))
        names += [(a, wiki.link(r, a)) for a in p.get("aliases") or [] if len(a) >= 4]
    return names


def overview_for(model, sysmsg, wiki, rel, kind, title, ctx):
    """The model's 2 to 4 sentences for the top of one page, only those whose numbers are
    in what it was given, linked to the pages they name; None if it gave none."""
    what = ("explain this topic for the business, from the points its documents make; each point names its "
            "document and date, figures from different documents are never combined into one statement, and "
            "where two points disagree the newer document's holds (give the older value only as what it was)"
            if kind == "topic" else "say who or what this is for the business and what currently holds")
    a = model.ask(sysmsg, f"Write 2 to 4 short sentences for the top of the wiki page \"{title}\": {what}. "
                  "Use only the information below and add nothing else. Keep names, amounts and dates "
                  "exactly as given. Where a value changed, give the current one and what it was before.\n\n"
                  + ctx, PROSE_SCHEMA, max_tokens=500, kind="overview")
    nums = F.numbers_in(ctx)
    keep = [s for s in (a.get("sentences") or []) if clean_line(s) and not F.ungrounded_numbers(s, nums)]
    if not keep:
        return None
    names = link_names(wiki, rel)
    return " ".join(LP.linked(s, names, 600) for s in keep)


# When a page's overview is not written again (wiki.config.json "overviews"). An overview
# is written from prose_context(), a text code builds; archive/overview-basis.json keeps,
# per page, the text its overview was last written from. A page is skipped when:
#   - a person edited its engine block (the overview could not be placed anyway; always);
#   - the text is the same as last time ("skipUnchanged", on by default);
#   - only new roles came in, each one the page already had ("skipImmaterial", entity
#     pages only, off by default), e.g. one more invoice from the same supplier;
#   - Jev, asked with the old overview and both texts, is at least "jevBar" sure that the
#     overview still holds ("jev", off by default; only with Claude, a TypeSafe key, and
#     Claude paid per use with an API key unless "jevOnPlan").
# It is always written again when it has none, when a standing term (a price, a fee,
# payment terms, ...: F.COMPARED) changed, or when it states a number that is no longer
# current; and whenever anything goes wrong. A skip keeps the basis where it was, so small
# changes add up and are judged together next time.
OVERVIEW_DEFAULTS = {"skipUnchanged": True, "skipImmaterial": False, "jev": False, "jevBar": 0.9,
                     "jevOnPlan": False}
OVERVIEW_BASIS = "archive/overview-basis.json"
BEFORE_RE = re.compile(r"\s*\(before: [^()]*\)")
JEV_OVERVIEW_Q = ("Is the current overview still accurate and complete enough for the top of this wiki page, "
                  "given the facts now, so that rewriting it would add nothing important? Answer no if any value "
                  "it states has changed, if an important new fact, relationship or role is missing, or if "
                  "anything it says is no longer true.")


def overview_settings(config):
    o = config.get("overviews") if isinstance(config.get("overviews"), dict) else {}
    out = dict(OVERVIEW_DEFAULTS)
    for k in ("skipUnchanged", "skipImmaterial", "jev", "jevOnPlan"):
        if isinstance(o.get(k), bool):
            out[k] = o[k]
    try:
        out["jevBar"] = min(0.99, max(0.5, float(o.get("jevBar", OVERVIEW_DEFAULTS["jevBar"]))))
    except (TypeError, ValueError):
        pass
    return out


def load_basis():
    try:
        with open(os.path.join(WIKI_DIR, OVERVIEW_BASIS), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("ctx"), str)} \
        if isinstance(data, dict) else {}


def save_basis(basis):
    path = os.path.join(WIKI_DIR, OVERVIEW_BASIS)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(basis, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def standing_terms(wiki, claims, rel):
    """The page's current standing terms (F.COMPARED), by what they are about."""
    title_of = lambda r: wiki.pages.get(r, {}).get("title") or r  # noqa: E731
    return {LP.fact_label(g[0], title_of): str(g[0].get("value") or "")
            for g in claims.facts(rel) if g[0]["attribute"] in F.COMPARED}


def page_roles(claims, rel):
    return sorted({r["value"] for r in claims.records("_doc", rel) if r.get("value")})


def current_numbers(ctx):
    """The numbers a prose context states as current (a "(before: ...)" value is not)."""
    return F.numbers_in(BEFORE_RE.sub("", ctx))


def overview_basis(wiki, claims, rel, kind, ctx):
    rec = {"ctx": ctx, "kind": kind, "at": datetime.date.today().isoformat()}
    if kind != "topic":
        rec.update(terms=standing_terms(wiki, claims, rel), roles=page_roles(claims, rel),
                   ctx_no_roles=prose_context(wiki, claims, rel, kind, roles=False))
    return rec


def overview_gate(wiki, claims, rel, kind, ctx, body, old, settings):
    """("write" | "skip" | "jev", why) for one page the run touched (see OVERVIEW_DEFAULTS)."""
    if LP.block_edited(body):
        return "skip", "edited"
    if not old:
        return "write", "no basis"
    existing = LP.block_section(body, "Overview")
    if not existing.strip():
        return "write", "no overview"
    if kind != "topic":
        before, now = old.get("terms") if isinstance(old.get("terms"), dict) else {}, standing_terms(wiki, claims, rel)
        if any(before.get(k) != v for k, v in now.items()):
            return "write", "terms changed"
    said_d, said_n = F.numbers_in(existing)
    was_d, was_n = current_numbers(old["ctx"])
    now_d, now_n = current_numbers(ctx)
    if (said_d & was_d) - now_d or (said_n & was_n) - now_n:
        return "write", "a number it states is no longer current"
    if settings["skipUnchanged"] and ctx == old["ctx"]:
        return "skip", "unchanged"
    if (settings["skipImmaterial"] and kind == "entity" and isinstance(old.get("roles"), list)
            and old.get("ctx_no_roles") == prose_context(wiki, claims, rel, kind, roles=False)
            and set(page_roles(claims, rel)) <= set(old["roles"])):
        return "skip", "immaterial"
    if settings.get("jev_key"):
        return "jev", ""
    return "write", "changed"


def jev_holds(title, existing, old_ctx, ctx, key):
    """Jev's probability that the existing overview still holds; None on any failure."""
    import wiki_jev
    state = (f"Page: {title}\nCurrent overview:\n{existing}\nFacts it was written from:\n{old_ctx}\n"
             f"Facts now:\n{ctx}")[:wiki_jev.STATE_CHARS]
    try:
        out = wiki_jev.call(state, {"q": {"type": "noul", "instructions": JEV_OVERVIEW_Q}}, key)
        p = (out["answers"].get("q") or {}).get("noul")
    except Exception as e:  # never a reason to fail the run: the overview is written
        log(f"Jev could not judge the overview of {title}: {e}")
        return None
    return float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else None


def jev_key_for_overviews(settings, config):
    """The TypeSafe key, when Jev may judge overviews in this run; otherwise ""."""
    if not settings["jev"] or Run.reader != "claude":
        return ""
    import wiki_jev
    if not wiki_jev.available(config):
        return ""
    if not (os.environ.get("ANTHROPIC_API_KEY") or settings["jevOnPlan"]):
        return ""   # on a Claude plan an overview costs nothing extra: Jev would only add cost
    return wiki_jev.get_key()


def refresh(model, wiki, claims, sysmsg, touched):
    """A short overview, written by the model from what is recorded, on every page this
    run touched, unless the gate above finds it would add nothing; code rebuilds the rest
    of each page's block. A failure leaves a page's previous overview in place and never
    fails the run. With Claude (Run.lanes > 1) the overviews are asked for all at once,
    Run.lanes at a time."""
    items = [(r, k) for r, k in touched.items() if os.path.exists(os.path.join(WIKI_DIR, r))]
    Run.current = ""
    settings = dict(Run.overviews or OVERVIEW_DEFAULTS)
    try:
        settings["jev_key"] = jev_key_for_overviews(settings, load_config())
    except Exception as e:
        log(f"Jev will not judge overviews: {e}")
        settings["jev_key"] = ""
    basis = load_basis()
    contexts, gate = {}, {}
    counts = {"written": 0, "edited": 0, "unchanged": 0, "immaterial": 0, "jev": 0, "failed": 0}
    for rel, kind in items:
        engine_topic = kind != "topic" or wiki.pages.get(rel, {}).get("engine")
        try:
            contexts[rel] = prose_context(wiki, claims, rel, kind) if engine_topic else ""
        except Exception as e:
            log(f"overview of {rel} skipped: {e}")
            contexts[rel] = ""
        if not contexts[rel]:
            continue
        try:
            body = split_frontmatter(read(rel))[1]
            gate[rel] = overview_gate(wiki, claims, rel, kind, contexts[rel], body, basis.get(rel), settings)
        except Exception as e:  # when in doubt, write
            log(f"overview of {rel}: {e}; writing it")
            gate[rel] = ("write", "check failed")
    asking_jev = [rel for rel, (g, _) in gate.items() if g == "jev"]
    if asking_jev:
        key = settings.get("jev_key") or ""
        import wiki_jev
        say("write", f"Jev is checking whether {len(asking_jev)} overviews still hold")

        def judge(rel):
            body = split_frontmatter(read(rel))[1]
            return jev_holds(wiki.pages.get(rel, {}).get("title") or rel, LP.block_section(body, "Overview"),
                             basis[rel]["ctx"], contexts[rel], key)
        with concurrent.futures.ThreadPoolExecutor(max_workers=wiki_jev.PARALLEL) as pool:
            for rel, p in zip(asking_jev, pool.map(judge, asking_jev)):
                gate[rel] = ("skip", "jev") if p is not None and p >= settings["jevBar"] else ("write", "jev says rewrite")
    asked = [(rel, kind) for rel, kind in items if gate.get(rel, ("",))[0] == "write"]
    for rel, (g, why) in gate.items():
        if g == "skip":
            counts[why] += 1
    ready, stopped = {}, False
    if Run.cap and isinstance(model, ClaudeModel) and model.claude.spent() >= Run.cap:
        log(f"overviews left as they are: the batch reached its spending cap (${Run.cap:g})")
        stopped = True
    elif Run.lanes > 1 and asked:
        say("write", f"Claude is writing the overviews of {len(asked)} pages, {Run.lanes} at a time")
        with concurrent.futures.ThreadPoolExecutor(max_workers=Run.lanes) as pool:
            futs = {pool.submit(overview_for, model, sysmsg, wiki, rel, kind,
                                wiki.pages.get(rel, {}).get("title") or rel, contexts[rel]): rel for rel, kind in asked}
            for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                rel = futs[fut]
                try:
                    ready[rel] = fut.result()
                except ModelUnavailable as e:
                    if not stopped:
                        log(f"overviews stopped: {e}")
                    stopped = True
                except Exception as e:
                    log(f"overview of {rel} skipped: {e}")
                say("write", "writing the page overviews", n=n, of=len(asked))
    changed_basis = False
    try:
        for n, (rel, kind) in enumerate(items, 1):
            title = wiki.pages.get(rel, {}).get("title") or rel
            ctx = contexts[rel]
            overview = ready.get(rel)
            wanted = gate.get(rel, ("",))[0] == "write"
            if wanted and Run.lanes <= 1 and not stopped:
                say("write", f"writing the overview of {clean_line(title, 50)}", n=n, of=len(items))
                model.deadline = time.time() + Run.prose_budget
                try:
                    overview = overview_for(model, sysmsg, wiki, rel, kind, title, ctx)
                except (ModelUnavailable, DocTimeout) as e:
                    log(f"overviews stopped: {e}")
                    break
                except Exception as e:
                    log(f"overview of {rel} skipped: {e}")
                finally:
                    model.deadline = None
            if wanted:
                counts["written" if overview else "failed"] += 1
            fm, body = split_frontmatter(read(rel))
            content = topic_block(wiki, claims, rel, overview) if kind == "topic" else entity_block(wiki, claims, rel, overview)
            engine_page = wiki.pages.get(rel, {}).get("engine")
            new_body, outcome = LP.put_block(body, content, "top" if (kind != "topic" or engine_page) else "end")
            if outcome == "written":
                write(rel, ("---\n" + fm.strip("\n") + "\n---\n" if fm else "") + new_body)
                page_event(rel, False, wiki)
            if overview and outcome in ("written", "unchanged"):
                try:
                    basis[rel] = overview_basis(wiki, claims, rel, kind, ctx)
                    changed_basis = True
                except Exception as e:
                    log(f"overview basis of {rel} not kept: {e}")
    finally:
        if changed_basis:
            try:
                save_basis(basis)
            except OSError as e:
                log(f"could not keep {OVERVIEW_BASIS}: {e}")
        skipped = counts["edited"] + counts["unchanged"] + counts["immaterial"] + counts["jev"]
        Run.overview_counts = counts
        if gate:
            log(f"overviews: {counts['written']} written, {skipped} skipped (edited {counts['edited']}, "
                f"unchanged {counts['unchanged']}, immaterial {counts['immaterial']}, jev {counts['jev']}), "
                f"{counts['failed']} failed")


def at_a_glance(wiki, claims):
    """The overview page's engine block: what the wiki holds, built by code."""
    rel = "wiki/overview.md"
    if not os.path.exists(os.path.join(WIKI_DIR, rel)):
        return
    link = link_of(wiki)
    docs = {}
    for r in claims.records("_doc"):
        if r.get("summary") in wiki.pages and r["summary"].startswith("wiki/sources/"):
            docs.setdefault(r["summary"], r)
    latest = sorted(docs.values(), key=F.Claims.order, reverse=True)[:5]
    count = {}
    for r in claims.records("_doc"):
        if r["page"] in wiki.pages:
            count.setdefault(r["page"], set()).add(r.get("source"))
    parties = sorted(count, key=lambda p: (-len(count[p]), p))[:6]
    topics = [r for r, _ in wiki.topics()][:12]
    decisions = sorted((r for r in wiki.pages if r.startswith("wiki/decisions/")), reverse=True)[:5]
    try:
        waiting = len(re.findall(r"^## \[", read("wiki/_review.md"), re.M))
    except OSError:
        waiting = 0
    n_sources = len([r for r in wiki.pages if r.startswith("wiki/sources/")])
    lines = ["## At a glance", "", "_Kept up to date by the wiki on every run._", ""]
    if n_sources:
        lines.append(f"- **Documents read:** {n_sources}" + (". Latest: " + ", ".join(
            link(r["summary"]) + (f" ({r['doc_date']})" if r.get("doc_date") else "") for r in latest) if latest else ""))
    if parties:
        lines.append("- **Named most often:** " + ", ".join(link(p) for p in parties))
    if topics:
        lines.append("- **Topics:** " + ", ".join(link(t) for t in topics))
    if decisions:
        lines.append("- **Latest decisions:** " + ", ".join(link(d) for d in decisions))
    lines.append(f"- **Review queue:** {waiting} item(s) waiting for a person" if waiting else "- **Review queue:** empty")
    fm, body = split_frontmatter(read(rel))
    new_body, outcome = LP.put_block(body, "\n".join(lines), "end")
    if outcome == "written":
        write(rel, ("---\n" + fm.strip("\n") + "\n---\n" if fm else "") + new_body)
        page_event(rel, False, wiki)


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


# The example rules HOUSE-RULES.md has shipped with, above its first heading. A wiki keeps
# its copy through every update, so they may still be there: they are examples, not the
# business's rules, and one of them ("Never record staff salaries") contradicts what
# employee pages now hold. Only these exact lines, and only above the first "##" heading
# (where the examples stand), are left out; every line the owner wrote is read.
STOCK_EXAMPLES = {
    '- Our company is Example Trading Ltd. "We", "us" and "the company" mean Example Trading.',
    "- Customers are filed under `companies/customers/`, suppliers under `companies/suppliers/`.",
    "- Example Logistics is a supplier, even when a document calls it a partner.",
    "- Amounts are in MYR unless a source says otherwise. Write them as `RM 1,250.00`.",
    "- Never record staff salaries in the wiki. Note only that a payslip exists.",
    "- Name staff as on their IC, with the name they go by in brackets.",
    "- Our financial year ends on 31 December.",
}


def house_rules():
    try:
        text = read("HOUSE-RULES.md")
    except OSError:
        return ""
    lines, examples = [], True
    for line in text.split("\n"):
        examples = examples and not line.startswith("##")
        if not (examples and line.rstrip() in STOCK_EXAMPLES):
            lines.append(line)
    return "\n".join(lines)[:HOUSE_RULES_CHARS]


def load_config():
    try:
        with open(os.path.join(WIKI_DIR, "wiki.config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def memory_gb():
    """This Mac's memory in GB (0 if it cannot be told)."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30
    except (ValueError, OSError, AttributeError):
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip()) / 2 ** 30
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def write_list(path, items):
    if not path:
        return
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("".join(p + "\n" for p in items))
        os.replace(tmp, path)
    except OSError as e:
        log(f"could not write {path}: {e}")


def run(server=None, only_docs=None, packets_only=False, reader="local", tried_path="", capped_path="",
        only_packets=None):
    docs = [] if packets_only else pending("raw/_intake")
    if only_docs is not None:
        docs = [d for d in docs if d in only_docs]
    packets = [] if only_docs is not None else [p for p in pending("raw/inbox") if p.endswith((".md", ".markdown"))]
    if only_packets is not None:
        packets = [p for p in packets if p in only_packets]
    if not docs and not packets:
        write_list(capped_path, [])
        write_list(tried_path, [])
        return 0
    docs_given = list(docs)
    config = load_config()
    cfg = config.get("localModel") or {}
    Run.company = clean_line(config.get("company"), 120)
    Run.doc_budget = float(cfg.get("docBudgetSeconds") or DOC_BUDGET_SECONDS)
    Run.prose_budget = float(cfg.get("proseBudgetSeconds") or PROSE_BUDGET_SECONDS)
    sysmsg = system_prompt(house_rules())
    Run.reader = reader
    Run.overviews = overview_settings(config)
    Run.routing, Run.routed = {}, {}
    if reader == "claude":
        # The fast pipeline: settings under "fast" in wiki.config.json; by default Claude
        # reads three documents for every batch the classic path would read at once (the
        # reading speed, parallelBatches: 6 unless the owner chose less, so 18).
        fast = config.get("fast") if isinstance(config.get("fast"), dict) else {}
        figures = wiki_settings.effective(config)
        try:
            Run.lanes = int(fast.get("lanes") or 3 * figures["parallelBatches"])
        except (TypeError, ValueError):
            Run.lanes = 3 * figures["parallelBatches"]
        Run.lanes = max(1, min(Run.lanes, 18))
        # Each document read at once holds a Claude process and its answer in memory:
        # about one and a half per GB (an 8 GB Mac reads at most 12 at once).
        ram = memory_gb()
        if ram and Run.lanes > max(3, int(1.5 * ram)):
            log(f"reading {max(3, int(1.5 * ram))} documents at once, not {Run.lanes}: "
                f"this Mac has {ram:.0f} GB of memory")
            Run.lanes = max(3, int(1.5 * ram))
        # The batch's spending cap (the mode's, or the owner's own figure), as for a classic
        # batch: no new call starts once Claude's reported cost reaches it; one call may
        # spend at most the smaller of it and $2.50.
        Run.cap = float(figures["maxSpendPerBatchUsd"])
        chosen = str(fast.get("model") or "sonnet")   # "default": whatever Claude itself defaults to
        model = ClaudeModel({"model": "" if chosen == "default" else chosen, "budget": min(Run.cap, 2.5)})
        # Optional, per kind of call: the overviews' own model ("overviewModel", e.g. "haiku"),
        # an effort for the reading and for the overviews ("effort", "overviewEffort": low,
        # medium, high, ...; none: Claude's default), and the reading instructions in the
        # system prompt so that Claude can cache them ("cacheInstructions"). The models and
        # efforts are off by default: each changes what Claude writes, so it is turned on once a
        # gold-set run shows it holds. The cached instructions are on (false turns them off): a
        # real gold-set run read the same with them, for about a tenth less.
        Run.models = {"overview": str(fast["overviewModel"])} if fast.get("overviewModel") else {}
        Run.efforts = {k: str(fast[f]) for k, f in (("read", "effort"), ("overview", "overviewEffort"))
                       if fast.get(f) in ("low", "medium", "high", "xhigh", "max")}
        Run.instructions_in_system = fast.get("cacheInstructions") is not False
        Run.routing = routing_settings(fast, config)
        if Run.routing["enabled"]:
            # The lighter reader's model; a thin answer is read again with the reader's own
            # model and effort.
            Run.models["read-fast"] = Run.routing["fastModel"]
            if "read" in Run.efforts:
                Run.efforts["reread"] = Run.efforts["read"]
            log("choosing a reader for each document: the lighter one (" + Run.routing["fastModel"] + ") "
                + ("where code or Jev finds it can" if Run.routing["jev_key"] else "where code finds it can"))
        else:
            Run.routing = {}
        if Run.company:
            sysmsg += (f"\n\nThis wiki belongs to {Run.company}: when a document names it, that is the "
                       "business itself.")
    else:
        Run.lanes = 1
        model = Model(url=server, cfg=cfg)
    label_ = "fast" if reader == "claude" else "local"
    wiki_events.emit("claude-start", label=label_)
    t0 = time.time()
    ok = True
    claims = F.Claims(os.path.join(WIKI_DIR, "archive", "claims.jsonl"))
    touched = {}
    try:
        wiki = Wiki()
        if reader == "claude":
            ok = read_with_claude(model, wiki, claims, docs, sysmsg, touched)
            docs = []
        for kind, items, fn in (("document", docs, process_document), ("packet", packets, process_packet)):
            for src in items:
                in_flight(src, True)
                try:
                    fn(model, wiki, claims, src, sysmsg, touched)
                except ModelUnavailable:
                    raise
                except Exception as e:  # one bad file never stops the others
                    ok = False
                    log(f"{src}: {e}")
                    wiki_events.emit("error", file=src, msg=f"{os.path.basename(src)}: {e}")
                in_flight(src, False)
        # Every file is done; what follows can only improve pages, never fail a file. The
        # runner hears which documents the spending cap left (no try), and which were tried.
        write_list(capped_path, sorted(Run.capped))
        write_list(tried_path, [d for d in docs_given if d not in Run.capped] + packets)
        try:
            refresh(model, wiki, claims, sysmsg, touched)
            at_a_glance(wiki, claims)
        except ModelUnavailable as e:
            log(f"overviews stopped: {e}")
        except Exception as e:
            log(f"overviews skipped: {e}")
        try:
            # The company, people and topic pages this run touched take their places in the
            # menu now, so the site rebuilt after this batch shows them there.
            counts = wiki_menu.label([r for r, k in touched.items() if k in ("entity", "topic")], claims)
            if counts["changed"]:
                log(f"placed {counts['changed']} page(s) in the menu")
        except Exception as e:   # the menu never fails a run: the runner's tidy step labels again
            log(f"menu labels skipped: {e}")
    except ModelUnavailable as e:
        log(str(e))
        wiki_events.emit("claude-end", label=label_, ok=False, detail=str(e))
        write_list(Run.why_path, [str(e)])   # the runner's notice says what it was
        return 2
    finally:
        model.stop()
        claims.save()
    stats = model.stats
    secs = round(time.time() - t0)
    oc = Run.overview_counts
    overviews = (f", overviews {oc['written']} written, "
                 f"{oc['edited'] + oc['unchanged'] + oc['immaterial'] + oc['jev']} skipped" if oc else "")
    if reader == "claude":
        wiki_events.emit("claude-end", label=label_, ok=True, turns=stats["calls"],
                         cost=round(stats["cost"], 4) if stats["costed"] else None,
                         detail=f"{secs}s, {stats['calls']} Claude calls, {Run.lanes} at a time"
                                + (f", {stats['failed']} failed" if stats["failed"] else "") + overviews)
        log(f"Claude's calls: {model.claude.summary()}")
        if Run.routing:
            log(f"routing: {routing_summary()}")
    else:
        wiki_events.emit("claude-end", label=label_, ok=True, turns=stats["calls"], cost=0,
                         detail=f"{secs}s, {stats['prompt_tokens']} tokens read, "
                                f"{stats['completion_tokens']} written" + overviews)
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
    ap.add_argument("--reader", choices=["local", "claude"], default="local",
                    help="run: who reads the documents; claude is the fast pipeline (scripts/wiki_claude.py)")
    ap.add_argument("--packet-list", default="", help="run: with --packets, only the packets listed in this file")
    ap.add_argument("--tried", default="", help="run: write the documents it tried to this file")
    ap.add_argument("--capped", default="", help="run: write the documents a spending cap left unread to this file")
    ap.add_argument("--inflight", default="", help="run: keep the files being read right now in this file")
    ap.add_argument("--alone", default="", help="run: read the documents listed in this file first, one at a time")
    ap.add_argument("--why", default="", help="run: if the model or Claude cannot run (exit 2), say why in this file")
    a = ap.parse_args(argv)
    wiki_netguard.install()
    os.chdir(WIKI_DIR)
    server = a.server or os.environ.get("WIKI_LOCAL_SERVER_URL")  # tests
    if a.command == "check":
        return check(server)

    def paths_in(path):
        """The paths listed in a file, one per line, exactly (a name may end in a space)."""
        with open(path, encoding="utf-8") as f:
            return {line.rstrip("\n") for line in f if line.rstrip("\n")}

    only_docs = paths_in(a.docs) if a.docs else None
    only_packets = paths_in(a.packet_list) if a.packet_list else None
    Run.inflight_path, Run.why_path = a.inflight, a.why
    write_list(a.inflight, [])
    if a.alone:
        try:
            Run.alone = paths_in(a.alone)
        except OSError:
            pass
    try:
        return run(server, only_docs=only_docs, packets_only=a.packets, reader=a.reader, tried_path=a.tried,
                   capped_path=a.capped, only_packets=only_packets)
    except SystemExit as e:
        if e.code != 143:
            raise
        # Stopped (SIGTERM), its cleanup done: leave now, without waiting for a reading
        # thread that may be stuck in a call that will never return.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(143)


if __name__ == "__main__":
    sys.exit(main())
