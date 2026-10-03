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
    run applies to the pages concerned, like any packet.

An item:
  {"id": "RV-20261003-supplier-terms", "created": "2026-10-03",
   "kind": "contradiction" | "uncertain" | "missing" | "unreadable",
   "file": "raw/general/contract.pdf",          the document it is about
   "question": "...", "context": "...",          what each source says, with its path
   "pages": ["wiki/companies/acme.md"],          pages the answer changes
   "options": [{"key": "A", "label": "...", "effect": "..."}, ...],
   "recommended": "A",                           the writer's own pick, if any
   "jev": {...}, "answer": {...}}                added by scoring and answering

Usage (the runner):
  wiki_review.py park --file raw/_intake/x.jpg --to raw/_needs-review/x.jpg --attempts 2 --engine claude
  wiki_review.py notes --batch FILE     the owner's notes for documents in a batch, for the prompt
  wiki_review.py tidy                   forget notes for files no longer waiting

Standard library only.
"""
import argparse
import datetime
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
ID_RE = re.compile(r"^RV-[A-Za-z0-9][A-Za-z0-9-]{0,95}$")
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
    if isinstance(raw.get("jev"), dict):
        item["jev"] = raw["jev"]
    if isinstance(raw.get("answer"), dict):
        item["answer"] = raw["answer"]
    return item


def load(folder, name):
    try:
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            return normalise(json.load(f), name)
    except (OSError, ValueError):
        return None


def list_open():
    """Open items, oldest first. A file being written, or one that is not a valid item,
    is left out (and stays where it is)."""
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
    return out


def list_done(limit=40):
    try:
        names = [n for n in os.listdir(DONE) if n.endswith(".json")]
    except OSError:
        return []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(DONE, n)), reverse=True)
    return [i for i in (load(DONE, n) for n in names[:limit]) if i]


def get_open(iid):
    if not ID_RE.match(iid or ""):
        return None
    return load(OPEN, iid + ".json")


def save_open(item):
    """Rewrite an open item (scores added). Never brings back one answered meanwhile."""
    path = os.path.join(OPEN, item["id"] + ".json")
    with LOCK:   # an answer moves the file out under the same lock
        if os.path.exists(path):
            write_json(path, item)


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
    """The decision as an Update Packet in raw/inbox/; returns its path."""
    q = item["question"]
    pages = ", ".join(item["pages"]) or "the pages named in the review entry"
    if option:
        answer = option["label"] + (f" — {option['effect']}" if option.get("effect") else "")
    else:
        answer = "(in the owner's words) " + clip(text, 1500)
    if option and text:
        answer += " The owner added: " + clip(text, 1500)
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
    if item.get("context"):
        lines.append(f"- Context: {clip(item['context'], 1500)}")
    if item.get("file"):
        lines.append(f"- Source document: {item['file']}")
    lines += [
        "- Apply the answer to the pages above, and add a line under the matching "
        "wiki/_review.md entry saying it was resolved, with the answer and the date.",
        "**Supersedes:** whatever those pages said that this answer replaces",
        "**Open questions:** none",
        "",
    ]
    os.makedirs(os.path.join(WIKI_DIR, "raw", "inbox"), exist_ok=True)
    name = f"update-{today()}-review-{slug(item['id'][3:], 60)}.md"
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
    with LOCK:
        item = get_open(iid)
        if not item:
            return {"ok": False, "message": "That review item is no longer open."}
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
                if text:
                    add_note(dest, text)
                queued = True
                message = f"{os.path.basename(src)} is back in the queue."
                wiki_events.emit("routed", file=src, to=dest)
            elif action == "setaside":
                dest = wiki_rel(place(os.path.join(WIKI_DIR, src),
                                      os.path.join(WIKI_DIR, "raw", "_set-aside", os.path.basename(src))))
                message = f"{os.path.basename(src)} was set aside in {os.path.dirname(dest)}/."
            else:
                message = f"{os.path.basename(src)} stays in Needs review."
        else:
            packet = wiki_rel(write_packet(item, option, text, by))
            queued = True
            message = "The answer goes into the wiki on the next run."
            wiki_events.emit("upload", file=packet, name=os.path.basename(packet), queue="inbox",
                             bytes=os.path.getsize(os.path.join(WIKI_DIR, packet)), via="review")
        item["answer"] = {"key": option["key"] if option else "", "label": option["label"] if option else "",
                          "text": clip(text, 1500), "by": by, "at": stamp()}
        item["status"] = "answered"
        write_json(os.path.join(DONE, item["id"] + ".json"), item)
        try:
            os.unlink(os.path.join(OPEN, item["id"] + ".json"))
        except OSError:
            pass
        conf = (item.get("jev") or {}).get("confidence") if by == "jev" else None
        wiki_events.emit("review-answered", id=item["id"], file=item["file"], by=by,
                         answer=item["answer"]["label"] or clip(text, 120), confidence=conf)
        return {"ok": True, "message": message, "queued": queued}


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
