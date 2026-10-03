#!/usr/bin/env python3
"""wiki_review.py — the Review queue's questions, each answerable with one click.

wiki/_review.md stays the readable record of everything that needs an eye. Next to it,
each question the owner can settle is an item: a JSON file in review/open/ while it waits,
moved to review/done/ with its answer once settled. Items come from two places:

  - Claude, while filing a document, for a contradiction or an uncertain fact
    (engine/prompts/intake.md says how);
  - the runner, for a file set aside after failing twice (`park` below).

The Upload page lists them in its Review tab (scripts/wiki_server.py), with Jev's scores
when a TypeSafe key is set (scripts/wiki_jev.py). An answer is applied like this:

  - a set-aside file: by code. Read it again (back into the queue, with the owner's note
    for the reader), set it aside for good (raw/_set-aside/, never read, nothing deleted),
    or leave it where it is;
  - anything else: an Update Packet in raw/inbox/ carrying the decision, which the next
    run applies to the pages concerned, like any packet. When the decision means someone
    must do something outside the wiki, the Claude session applying the packet also writes
    a suggested action (scripts/wiki_actions.py).

An answer can be changed: `reopen` moves an answered item back to review/open/ with the
earlier answer kept as "previous", and the new answer's packet says what it replaces. An
answer about a file that could not be read moved the file, so it cannot be reopened.

An item:
  {"id": "RV-20261003-supplier-terms", "created": "2026-10-03",
   "kind": "contradiction" | "uncertain" | "missing" | "unreadable",
   "file": "raw/general/contract.pdf",          the document it is about
   "question": "...", "context": "...",          what each source says, with its path
   "pages": ["wiki/companies/acme.md"],          pages the answer changes
   "options": [{"key": "A", "label": "...", "effect": "..."}, ...],
   "recommended": "A",                           the writer's own pick, if any
   "jev": {...}, "answer": {...},                added by scoring and answering
   "fileWas": "raw/_intake/contract.pdf",        where "file" was before the document was filed
   "previous": {...}, "reopened": "<time>"}      the earlier answer of a reopened item, which
                                                 auto-review then leaves to the owner

Where the document is now. An item is often written while its document still waits in
raw/_intake/, and the document is filed into raw/<project>/ afterwards, so "file" goes
stale. Two things keep the Review tab's links working: whoever files a document calls
`relocate` (the engine's fast reading, and Claude's stream for classic reading), and the
listings add "fileNow" (where the file is now, found by its name if it moved without a
relocate) and "page" (the wiki/sources/ page citing it). Both are worked out, never stored.

Item files change in three processes (the viewer, the runner, the follower of Claude's
stream), so every change takes a lock shared between processes (`changing`).

Usage (the runner):
  wiki_review.py park --file raw/_intake/x.jpg --to raw/_needs-review/x.jpg --attempts 2 --engine claude
  wiki_review.py notes --batch FILE     the owner's notes for documents in a batch, for the prompt
  wiki_review.py tidy                   forget notes for files no longer waiting

Standard library only.
"""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import secrets
import sys
import threading

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WIKI_DIR, "scripts"))
import wiki_events  # noqa: E402

OPEN = os.path.join(WIKI_DIR, "review", "open")
DONE = os.path.join(WIKI_DIR, "review", "done")
NOTES = os.path.join(WIKI_DIR, ".wiki-engine", "state", "review-notes.json")
LOCK_FILE = os.path.join(WIKI_DIR, ".wiki-engine", "state", "review.lock")
ID_RE = re.compile(r"^RV-[A-Za-z0-9][A-Za-z0-9-]{0,95}$")
# Fields worked out for the Upload page by the listings, never written to an item file.
DERIVED = ("fileNow", "page")
KINDS = ("contradiction", "uncertain", "missing", "unreadable")
KEYS = "ABCDEFGH"
# The converter's words (scripts/to_markdown.py), as the owner would say them.
HOW = {"copy": "plain text", "markitdown": "converted to text", "ocr+markitdown": "a scan, read with OCR",
       "ocr-image": "a picture, read with OCR", "image": "a picture that OCR could not read",
       "ERROR": "the converter failed on it"}
NOTES_SAID = {"image-little-text": "OCR found little or no text", "image-no-ocr": "no OCR on this Mac",
              "scanned-no-ocr": "a scan, and no OCR on this Mac",
              "heic-not-converted": "an iPhone photo that could not be converted"}
LOCK = threading.Lock()   # one answer at a time in this process


@contextlib.contextmanager
def changing(lock_file=LOCK_FILE, thread_lock=LOCK):
    """One change to the item files at a time: across this process's threads, and across
    the processes that change them. Without the shared lock file (a folder that cannot
    be written), the thread lock alone still holds."""
    with thread_lock:
        f = None
        try:
            os.makedirs(os.path.dirname(lock_file), exist_ok=True)
            f = open(lock_file, "a")
            fcntl.flock(f, fcntl.LOCK_EX)
        except OSError:
            if f:
                f.close()
            f = None
        try:
            yield
        finally:
            if f:
                fcntl.flock(f, fcntl.LOCK_UN)
                f.close()


def today():
    return datetime.date.today().isoformat()


def stamp():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def wiki_rel(path):
    return os.path.relpath(path, WIKI_DIR).replace(os.sep, "/")


def clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def slug(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return (s[:n].rstrip("-") or "item")


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------ items -------
def normalise(raw, name):
    """An item as the Upload page and Jev need it, or None if it cannot be answered.
    Items are written by Claude, so every field is checked, never trusted."""
    if not isinstance(raw, dict):
        return None
    iid = name[:-5] if name.endswith(".json") else name
    if not ID_RE.match(iid):
        return None
    question = clip(raw.get("question"), 400)
    options, seen = [], set()
    for i, o in enumerate(raw.get("options") or []):
        if len(options) >= len(KEYS):
            break
        if isinstance(o, str):
            o = {"label": o}
        if not isinstance(o, dict) or not clip(o.get("label"), 200):
            continue
        key = str(o.get("key") or "").strip().upper()[:2]
        if not re.fullmatch(r"[A-Z]{1,2}", key) or key in seen:
            key = next(k for k in KEYS if k not in seen)   # at most len(KEYS) options
        seen.add(key)
        opt = {"key": key, "label": clip(o.get("label"), 200), "effect": clip(o.get("effect"), 400)}
        if o.get("action") in ("retry", "setaside", "keep"):
            opt["action"] = o["action"]
        if o.get("auto") is False:
            opt["auto"] = False
        options.append(opt)
    if not question or len(options) < 2:
        return None
    kind = raw.get("kind") if raw.get("kind") in KINDS else "uncertain"
    pages = [p for p in (raw.get("pages") or []) if isinstance(p, str) and p.strip()][:10]
    item = {
        "id": iid,
        "created": str(raw.get("created") or "")[:10],
        "kind": kind,
        "file": str(raw.get("file") or "")[:500],
        "question": question,
        "context": str(raw.get("context") or "")[:4000],
        "pages": [p.strip()[:300] for p in pages],
        "options": options,
        "recommended": str(raw.get("recommended") or "").strip().upper()[:2],
    }
    for k in ("queued", "facts"):
        if isinstance(raw.get(k), (str, dict)):
            item[k] = raw[k]
    for k, n in (("fileWas", 500), ("reopened", 40)):
        if isinstance(raw.get(k), str) and raw[k]:
            item[k] = raw[k][:n]
    if isinstance(raw.get("jev"), dict):
        item["jev"] = raw["jev"]
    if isinstance(raw.get("answer"), dict):
        item["answer"] = raw["answer"]
    prev = earlier_answer(raw.get("previous"))
    if prev:
        item["previous"] = prev
    return item


def earlier_answer(prev):
    """The earlier answer of a reopened item, checked: it ends up in a packet."""
    if not isinstance(prev, dict) or not (clip(prev.get("label"), 200) or clip(prev.get("text"), 1500)):
        return None
    out = {"key": str(prev.get("key") or "").strip().upper()[:2], "label": clip(prev.get("label"), 200),
           "text": clip(prev.get("text"), 1500), "by": "jev" if prev.get("by") == "jev" else "owner",
           "at": str(prev.get("at") or "")[:40]}
    if isinstance(prev.get("jev"), dict):
        out["jev"] = prev["jev"]
    packet = prev.get("packet")
    if isinstance(packet, str) and packet.startswith("raw/inbox/") and ".." not in packet.split("/"):
        out["packet"] = packet[:500]
    return out


def load(folder, name):
    try:
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            return normalise(json.load(f), name)
    except (OSError, ValueError):
        return None


def list_open(places=True):
    """Open items, oldest first. A file being written, or one that is not a valid item,
    is left out (and stays where it is). places: add "fileNow" and "page"."""
    out = []
    try:
        names = sorted(n for n in os.listdir(OPEN) if n.endswith(".json"))
    except OSError:
        return out
    for n in names:
        item = load(OPEN, n)
        if item:
            out.append(item)
    out.sort(key=lambda i: (i["created"], i["id"]))
    return add_places(out) if places else out


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0   # answered or moved meanwhile


def list_done(limit=40, places=True):
    """Answered items, newest first."""
    try:
        names = [n for n in os.listdir(DONE) if n.endswith(".json")]
    except OSError:
        return []
    names.sort(key=lambda n: mtime(os.path.join(DONE, n)), reverse=True)
    out = [i for i in (load(DONE, n) for n in names[:limit]) if i]
    return add_places(out) if places else out


def count_done():
    try:
        return sum(1 for n in os.listdir(DONE) if n.endswith(".json"))
    except OSError:
        return 0


def get_open(iid):
    if not ID_RE.match(iid or ""):
        return None
    return load(OPEN, iid + ".json")


def save_open(item):
    """Add Jev's scores to an open item. Never brings back one answered meanwhile, and
    keeps whatever changed in it while Jev was scoring (a reopen, a relocate): only the
    scores are written into the file as it is now."""
    path = os.path.join(OPEN, item["id"] + ".json")
    with changing():   # an answer moves the file out under the same lock
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return   # answered meanwhile, or being written
        if isinstance(raw, dict) and isinstance(item.get("jev"), dict):
            raw["jev"] = item["jev"]
            write_json(path, raw)


# ----------------------------------------------------- where documents are now -------
def relocate(old, new):
    """A document was filed from `old` to `new` (paths relative to the wiki): every item
    about it, open or answered, now says `new`, and keeps `old` as "fileWas". Returns the
    number of items changed."""
    if not old or not new or old == new:
        return 0
    changed = 0
    with changing():
        for folder in (OPEN, DONE):
            try:
                names = [n for n in os.listdir(folder) if n.endswith(".json") and ID_RE.match(n[:-5])]
            except OSError:
                continue
            for n in names:
                path = os.path.join(folder, n)
                try:
                    with open(path, encoding="utf-8") as f:
                        raw = json.load(f)
                except (OSError, ValueError):
                    continue   # being written, or not an item: left alone
                if not isinstance(raw, dict) or raw.get("file") != old:
                    continue
                raw["file"], raw["fileWas"] = new, old
                try:
                    write_json(path, raw)
                except OSError as e:   # the document is filed either way; listings still find it
                    wiki_events.log(f"review item {n} not updated: {e}")
                    continue
                changed += 1
    return changed


def raw_index():
    """Every file under raw/ by its name: {name: [(path, mtime), ...]}. raw/inbox/ holds
    packets, never documents; hidden files and links are left out."""
    index = {}
    base = os.path.join(WIKI_DIR, "raw")
    for dirpath, dirs, files in os.walk(base):
        rel_dir = os.path.relpath(dirpath, WIKI_DIR).replace(os.sep, "/")
        dirs[:] = [d for d in dirs if not d.startswith(".") and f"{rel_dir}/{d}" != "raw/inbox"]
        for fn in files:
            full = os.path.join(dirpath, fn)
            if fn.startswith(".") or os.path.islink(full):
                continue
            index.setdefault(fn, []).append((f"{rel_dir}/{fn}", mtime(full)))
    return index


def safe_raw_path(path):
    """path if it names a regular file inside raw/ (a link out of it does not count)."""
    if not isinstance(path, str) or not path.startswith("raw/") or "\x00" in path or "\\" in path:
        return ""
    if any(p in ("", ".", "..") for p in path.split("/")):
        return ""
    full = os.path.join(WIKI_DIR, *path.split("/"))
    real, base = os.path.realpath(full), os.path.realpath(os.path.join(WIKI_DIR, "raw"))
    return path if real.startswith(base + os.sep) and os.path.isfile(real) else ""


def resolve_file(path, index):
    """Where an item's document is now: its own path if the file is there; else the one
    file under raw/ with the same name (of several: one already filed, outside raw/_intake/
    and raw/_needs-review/, then the newest); "" if none. index: raw_index(), or a
    callable returning it (built only when a file has moved)."""
    here = safe_raw_path(path)
    if here or not isinstance(path, str):
        return here
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name.startswith(".") or "\x00" in name:
        return ""
    if callable(index):
        index = index()
    found = (index or {}).get(name) or []
    if not found:
        return ""
    queued = ("raw/_intake/", "raw/_needs-review/")
    return max(found, key=lambda pm: (not pm[0].startswith(queued), pm[1], pm[0]))[0]


_SOURCES_CACHE = {}   # page path -> (mtime_ns, size, sources): pages are re-read only when they change
_LIST_ITEM_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\']|\'\')*)\'|([^,\s][^,]*)')


def _scalar(token):
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] == '"':
        try:
            return json.loads(token)
        except ValueError:
            return token[1:-1]
    if len(token) >= 2 and token[0] == token[-1] == "'":
        return token[1:-1].replace("''", "'")
    return token


def page_sources(text):
    """The `sources:` list of a page's frontmatter, inline ([a, "b"]) or one item a line."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    lines = text[3:end if end > 0 else len(text)].splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("sources:"):
            continue
        rest = line[len("sources:"):].strip()
        if rest.startswith("["):
            inner = rest[1:rest.rfind("]")] if "]" in rest else rest[1:]
            out = []
            for m in _LIST_ITEM_RE.finditer(inner):
                if m.group(1) is not None:
                    out.append(_scalar('"' + m.group(1) + '"'))
                elif m.group(2) is not None:
                    out.append(m.group(2).replace("''", "'"))
                elif m.group(3).strip():
                    out.append(m.group(3).strip())
            return out
        if rest:
            return [_scalar(rest)]
        out = []
        for nxt in lines[i + 1:]:
            s = nxt.strip()
            if not s.startswith("- "):
                if s:
                    break
                continue
            out.append(_scalar(s[2:]))
        return out
    return []


def sources_index():
    """{document path: [pages citing it in their `sources:`]} over wiki/sources/*.md."""
    folder = os.path.join(WIKI_DIR, "wiki", "sources")
    index, seen = {}, set()
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".md") and not n.startswith("."))
    except OSError:
        names = []
    for n in names:
        full = os.path.join(folder, n)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        page = "wiki/sources/" + n
        seen.add(page)
        hit = _SOURCES_CACHE.get(page)
        if not hit or hit[:2] != (st.st_mtime_ns, st.st_size):
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    head = f.read(16384)
            except OSError:
                continue
            hit = (st.st_mtime_ns, st.st_size, page_sources(head))
            _SOURCES_CACHE[page] = hit
        for src in hit[2]:
            if isinstance(src, str) and src:
                index.setdefault(src.strip(), []).append((len(hit[2]), page))
    for page in [p for p in list(_SOURCES_CACHE) if p not in seen]:
        _SOURCES_CACHE.pop(page, None)
    # Of several pages citing a document, its own summary (the fewest sources) comes first.
    return {src: min(pages)[1] for src, pages in index.items()}


def add_places(items):
    """Add "fileNow" and "page" to listed items; raw/ and wiki/sources/ are read at most
    once for the whole listing."""
    raw = {}

    def lazy_raw():
        if "index" not in raw:
            raw["index"] = raw_index()
        return raw["index"]

    pages = None
    for it in items:
        it["fileNow"] = resolve_file(it.get("file") or "", lazy_raw)
        if it["fileNow"] and pages is None:
            pages = sources_index()
        it["page"] = (pages or {}).get(it["fileNow"], "") if it["fileNow"] else ""
    return items


def revision():
    """Changes whenever an item is added, scored or answered (for the Upload page)."""
    marks = []
    for d in (OPEN, DONE):
        try:
            marks.append(str(os.stat(d).st_mtime_ns))
        except OSError:
            marks.append("0")
    return "-".join(marks)


def count_open():
    try:
        return sum(1 for n in os.listdir(OPEN) if n.endswith(".json"))
    except OSError:
        return 0


def new_id(text):
    base = f"RV-{datetime.date.today():%Y%m%d}-{slug(text)}"
    iid, n = base, 1
    while any(os.path.exists(os.path.join(d, iid + ".json")) for d in (OPEN, DONE)):
        n += 1
        iid = f"{base}-{n}"
    return iid


# ---------------------------------------------------------- set-aside files -------
def mirror_of(path):
    """The converted text of a queued file, from wherever it was converted."""
    name = os.path.basename(path)
    cache = os.path.realpath(os.path.join(WIKI_DIR, "cache", "md"))
    for p in (os.path.join(cache, path[len("raw/"):] + ".md") if path.startswith("raw/") else "",
              os.path.join(cache, "_intake", name + ".md")):
        if p and os.path.realpath(p).startswith(cache + os.sep) and os.path.isfile(p):
            try:
                return open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                pass
    return ""


def split_mirror(text):
    fm, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            for line in text[4:end].splitlines():
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip()
            body = text[end + 4:]
    return fm, body.strip()


def review_notes_about(name, limit=6):
    """Lines of wiki/_review.md that mention the file (what Claude said about it)."""
    try:
        lines = open(os.path.join(WIKI_DIR, "wiki", "_review.md"), encoding="utf-8").read().splitlines()
    except OSError:
        return []
    return [clip(ln.lstrip("#- "), 300) for ln in lines if name in ln][-limit:]


def retried_before(name):
    """Whether the owner or Jev already sent this file back to be read again."""
    try:
        names = os.listdir(DONE)
    except OSError:
        return False
    for n in names:
        item = load(DONE, n)
        if not item or item["kind"] != "unreadable" or os.path.basename(item["file"]) != name:
            continue
        key = (item.get("answer") or {}).get("key")
        if any(o["key"] == key and o.get("action") == "retry" for o in item["options"]):
            return True
        if (item.get("answer") or {}).get("text"):
            return True
    return False


def park(queued, to, attempts, engine="claude"):
    """The item for a file the runner set aside after `attempts` failed tries."""
    name = os.path.basename(to)
    fm, body = split_mirror(mirror_of(queued))
    try:
        size = os.path.getsize(os.path.join(WIKI_DIR, to))
    except OSError:
        size = 0
    how = HOW.get(fm.get("method", ""), fm.get("method") or "it was never converted")
    if fm.get("note"):
        how += f" ({NOTES_SAID.get(fm['note'], fm['note'])})"
    found = clip(body, 600) if body else "no text"
    notes = review_notes_about(name)
    context = (f"{name} could not be added to the wiki after {attempts} tries, so it was set "
               f"aside in raw/_needs-review/. Converting it to text gave: {how}. "
               f"Text found in it: {found}.")
    if notes:
        context += " Notes in the review queue about it: " + " | ".join(notes)
    reader = "the model on this Mac" if engine == "local" else "Claude"
    options = []
    if not retried_before(name):
        look = "" if engine == "local" else ", with Claude looking at the original itself"
        options.append({"key": "A", "label": f"Read it again{look}", "action": "retry",
                        "effect": "It goes back into the queue and is read on the next run."})
    options.append({"key": "B", "label": "Set it aside: the wiki does not need it", "action": "setaside",
                    "effect": "It moves to raw/_set-aside/ and is never read. Nothing is deleted."})
    options.append({"key": "C", "label": "Leave it in Needs review for me to check", "action": "keep",
                    "auto": False, "effect": "Nothing changes; the file stays in raw/_needs-review/."})
    item = {
        "id": new_id(os.path.splitext(name)[0]),
        "created": today(),
        "kind": "unreadable",
        "file": to,
        "queued": queued,
        "question": f"{name} could not be read. What should happen to it?",
        "context": context,
        "pages": [],
        "options": options,
        "recommended": "A" if options[0]["action"] == "retry" else "",
        "facts": {"name": name, "type": os.path.splitext(name)[1].lower().lstrip(".") or "unknown",
                  "bytes": size, "conversion": how, "attempts": attempts, "reader": reader},
    }
    write_json(os.path.join(OPEN, item["id"] + ".json"), item)
    return item["id"]


# ------------------------------------------------------------------ notes -------
def read_notes():
    try:
        with open(NOTES, encoding="utf-8") as f:
            notes = json.load(f)
        return notes if isinstance(notes, dict) else {}
    except (OSError, ValueError):
        return {}


def add_note(path, text):
    notes = read_notes()
    notes[path] = clip(text, 1500)
    write_json(NOTES, notes)


def notes_block(paths):
    """The prompt lines carrying the owner's notes for these documents ("" if none)."""
    notes = read_notes()
    lines = [f"- {p}: {notes[p]}" for p in paths if notes.get(p)]
    if not lines:
        return ""
    return ("\nThe owner answered a review question about some of these documents and left a "
            "note for you. Treat each note as the owner's own description of the document, "
            "and say in its summary page that it came from the owner:\n" + "\n".join(lines) + "\n")


def tidy_notes():
    notes = read_notes()
    keep = {p: t for p, t in notes.items() if os.path.exists(os.path.join(WIKI_DIR, p))}
    if keep != notes:
        write_json(NOTES, keep)


# ---------------------------------------------------------------- answers -------
def place(src, dest):
    """Move src to dest without ever replacing a file; returns the path used."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    stem, ext = os.path.splitext(dest)
    for n in range(1, 1000):
        cand = dest if n == 1 else f"{stem} ({n}){ext}"
        try:
            os.link(src, cand)
        except FileExistsError:
            continue
        os.unlink(src)
        return cand
    raise OSError("too many files with this name")


def inside(path, folder):
    real = os.path.realpath(os.path.join(WIKI_DIR, path))
    base = os.path.realpath(os.path.join(WIKI_DIR, folder))
    return real.startswith(base + os.sep) and not os.path.islink(os.path.join(WIKI_DIR, path))


def decided_by(by, jev):
    if by == "jev" and isinstance(jev, dict):
        pct = round(100 * float(jev.get("confidence") or 0))
        return f"Jev auto-review (confidence {pct}%)"
    return "the owner"


def write_packet(item, option, text, by):
    """The decision as an Update Packet in raw/inbox/; returns its path. For a reopened
    item it also says which earlier answer this one replaces."""
    q = item["question"]
    pages = ", ".join(item["pages"]) or "the pages named in the review entry"
    if option:
        answer = option["label"] + (f" — {option['effect']}" if option.get("effect") else "")
    else:
        answer = "(in the owner's words) " + clip(text, 1500)
    if option and text:
        answer += " The owner added: " + clip(text, 1500)
    prev = item.get("previous") if isinstance(item.get("previous"), dict) else None
    replaces = supersedes = ""
    if prev:
        said = prev.get("label") or prev.get("text") or "an earlier answer"
        when = str(prev.get("at") or "")[:10]
        who = decided_by(prev.get("by"), prev.get("jev"))
        replaces = f"{clip(said, 400)} (decided by {who}{', ' + when if when else ''})"
        supersedes = (f"the earlier answer to this question, \"{clip(said, 300)}\" (decided by {who}"
                      f"{', ' + when if when else ''}), and whatever the pages say because of it")
    lines = [
        f"## [{today()}] update | general | Review answer: {clip(q, 90)}",
        "**Type:** decision",
        f"**Summary:** {decided_by(by, item.get('jev'))} answered the review question "
        f"\"{clip(q, 300)}\": {clip(answer, 400)}",
        f"**Affects pages:** {pages}",
        "**Details:**",
        f"- Review item: `{item['id']}`",
        f"- Question: {clip(q, 400)}",
        f"- Answer: {clip(answer, 1800)}",
        f"- Decided by: {decided_by(by, item.get('jev'))}",
    ]
    if replaces:
        lines.append(f"- This replaces the earlier answer: {replaces}")
    if item.get("context"):
        lines.append(f"- Context: {clip(item['context'], 1500)}")
    if item.get("file"):   # where it is now, if it was filed after the item was written
        lines.append(f"- Source document: {clip(resolve_file(item['file'], raw_index) or item['file'], 500)}")
    lines += [
        "- Apply the answer to the pages above, and add a line under the matching "
        "wiki/_review.md entry saying it was resolved, with the answer and the date."
        + (" Undo what the earlier answer changed where this one differs." if replaces else ""),
        f"**Supersedes:** {supersedes or 'whatever those pages said that this answer replaces'}",
        "**Open questions:** none",
        "",
    ]
    os.makedirs(os.path.join(WIKI_DIR, "raw", "inbox"), exist_ok=True)
    # The time in the name keeps packets in the order they were decided: a changed answer
    # sorts after the one it replaces (packets are applied oldest name first), and never
    # takes the name of a packet already archived.
    name = f"update-{today()}-{datetime.datetime.now():%H%M%S%f}-review-{slug(item['id'][3:], 50)}.md"
    tmp = os.path.join(WIKI_DIR, ".wiki-engine", "state", "uploads", f".review-{secrets.token_hex(6)}.part")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return place(tmp, os.path.join(WIKI_DIR, "raw", "inbox", name))


def answer(iid, key="", text="", by="owner"):
    """Settle an open item. Returns {"ok", "message", "queued"} (queued: the runner has
    something new to do)."""
    text = str(text or "").strip()
    key = str(key or "").strip().upper()
    with changing():
        item = get_open(iid)
        if not item:
            return {"ok": False, "message": "That review item is no longer open."}
        if by == "jev" and (item.get("reopened") or item.get("previous")):
            return {"ok": False, "message": "The owner opened that question again to answer it."}
        option = next((o for o in item["options"] if o["key"] == key), None)
        if key and not option:
            return {"ok": False, "message": "That answer is not one of the options."}
        if not option and not text:
            return {"ok": False, "message": "Choose an answer or write one."}
        queued, message = False, ""
        if item["kind"] == "unreadable":
            action = option.get("action") if option else "retry"   # a note means: read it again
            src = item["file"]
            here = os.path.isfile(os.path.join(WIKI_DIR, src)) and inside(src, "raw/_needs-review")
            if action in ("retry", "setaside") and not here:
                message = f"{os.path.basename(src)} was no longer in Needs review; nothing was moved."
            elif action == "retry":
                back = item.get("queued") if isinstance(item.get("queued"), str) else ""
                if not (back.startswith("raw/_intake/") and ".." not in back.split("/")):
                    back = "raw/_intake/" + os.path.basename(src)
                dest = wiki_rel(place(os.path.join(WIKI_DIR, src), os.path.join(WIKI_DIR, back)))
                item["fileWas"], item["file"] = src, dest   # its link follows it (and relocate, later)
                if text:
                    add_note(dest, text)
                queued = True
                message = f"{os.path.basename(src)} is back in the queue."
                wiki_events.emit("routed", file=src, to=dest)
            elif action == "setaside":
                dest = wiki_rel(place(os.path.join(WIKI_DIR, src),
                                      os.path.join(WIKI_DIR, "raw", "_set-aside", os.path.basename(src))))
                item["fileWas"], item["file"] = src, dest
                message = f"{os.path.basename(src)} was set aside in {os.path.dirname(dest)}/."
            else:
                message = f"{os.path.basename(src)} stays in Needs review."
        else:
            packet = wiki_rel(write_packet(item, option, text, by))
            queued = True
            message = "The answer goes into the wiki on the next run."
            if item.get("previous"):
                message += withdraw_earlier(item)
            wiki_events.emit("upload", file=packet, name=os.path.basename(packet), queue="inbox",
                             bytes=os.path.getsize(os.path.join(WIKI_DIR, packet)), via="review")
        item["answer"] = {"key": option["key"] if option else "", "label": option["label"] if option else "",
                          "text": clip(text, 1500), "by": by, "at": stamp()}
        if item["kind"] != "unreadable":
            item["answer"]["packet"] = packet
        item["status"] = "answered"
        write_json(os.path.join(DONE, item["id"] + ".json"), item)
        try:
            os.unlink(os.path.join(OPEN, item["id"] + ".json"))
        except OSError:
            pass
        conf = (item.get("jev") or {}).get("confidence") if by == "jev" else None
        wiki_events.emit("review-answered", id=item["id"], file=item.get("fileWas") or item["file"], by=by,
                         answer=item["answer"]["label"] or clip(text, 120), confidence=conf,
                         replaces=True if item.get("previous") else None)
        return {"ok": True, "message": message, "queued": queued}


def withdraw_earlier(item):
    """A changed answer: the earlier answer's packet is taken back if it has not been
    applied yet (it would only be undone), and the suggested actions drafted from the
    earlier answer are dismissed (nothing is deleted; they move to actions/done/).
    Returns a few words for the owner, or "". Called under the review lock."""
    said = []
    prev = item.get("previous") or {}
    packet = prev.get("packet") if isinstance(prev.get("packet"), str) else ""
    full = os.path.join(WIKI_DIR, *packet.split("/")) if packet.startswith("raw/inbox/") else ""
    if full and inside(packet, "raw/inbox") and os.path.isfile(full):
        try:
            os.unlink(full)
            said.append("the earlier answer was taken back before it was applied")
            wiki_events.emit("routed", file=packet, to="", msg="the answer it carried was changed")
        except OSError:
            pass
    try:
        import wiki_actions   # imports this module: imported here, when needed
        n = wiki_actions.dismiss_from(item["id"], "the review answer it came from was changed")
    except Exception as e:   # never lose an answer over its actions
        wiki_events.log(f"actions from {item['id']} not dismissed: {e}")
        n = 0
    if n:
        said.append(f"{n} action{'' if n == 1 else 's'} drafted from it {'was' if n == 1 else 'were'} dismissed")
    return (" " + "; ".join(said).capitalize() + ".") if said else ""


def reopen(iid):
    """Move an answered item back to review/open/ so the owner can change the answer.
    The earlier answer (with Jev's scores) is kept as "previous"; the next answer's packet
    says it replaces it. Returns {"ok", "message"}."""
    if not ID_RE.match(iid or ""):
        return {"ok": False, "message": "That review item was not found."}
    with changing():
        item = load(DONE, iid + ".json")
        if not item:
            return {"ok": False, "message": "That review item was not found among the answered ones."}
        if os.path.exists(os.path.join(OPEN, iid + ".json")):
            return {"ok": False, "message": "That review item is already open."}
        if item["kind"] == "unreadable":
            return {"ok": False, "message": "That answer moved the file (back into the queue, or set aside), "
                                            "so it cannot be changed here. The file's own question comes back "
                                            "if it still cannot be read."}
        old = item.pop("answer", None)
        if not isinstance(old, dict):
            return {"ok": False, "message": "That review item has no answer to change."}
        prev = earlier_answer(dict(old, jev=item.get("jev")) if isinstance(item.get("jev"), dict) else old)
        if prev:
            item["previous"] = prev
        item.pop("status", None)
        # Jev's scores stay, so the question is not sent again; a reopened item is never
        # answered automatically (scripts/wiki_jev.py): the owner reopened it to decide.
        item["reopened"] = stamp()
        write_json(os.path.join(OPEN, iid + ".json"), item)
        try:
            os.unlink(os.path.join(DONE, iid + ".json"))
        except OSError:
            pass
        wiki_events.emit("review-reopened", id=iid, file=item["file"],
                         answer=(prev or {}).get("label") or clip((prev or {}).get("text"), 120))
        return {"ok": True, "message": "The question is open again: choose the new answer. "
                                       "It replaces the earlier one on the next run."}


# -------------------------------------------------------------------- cli -------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Review items.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("park")
    p.add_argument("--file", required=True, help="where the file was queued")
    p.add_argument("--to", required=True, help="where it was set aside")
    p.add_argument("--attempts", type=int, default=2)
    p.add_argument("--engine", default="claude")
    n = sub.add_parser("notes")
    n.add_argument("--batch", required=True)
    sub.add_parser("tidy")
    a = ap.parse_args(argv)
    if a.cmd == "park":
        print(park(a.file, a.to, a.attempts, a.engine))
    elif a.cmd == "notes":
        with open(a.batch, encoding="utf-8") as f:
            print(notes_block([ln.strip() for ln in f if ln.strip()]), end="")
    elif a.cmd == "tidy":
        tidy_notes()
    return 0


if __name__ == "__main__":
    sys.exit(main())
