#!/usr/bin/env python3
"""wiki_actions.py — suggested actions: what someone must do outside the wiki.

Some findings need more than a page changed. A monthly tax deduction sent to the tax
authority without the employee's tax number must be corrected with the tax authority; a
licence must be renewed; an amount must be chased; a document must be signed and sent.
When the Claude session applying an Update Packet sees that a review answer means such a
thing (engine/prompts/ingest.md says how), it writes one suggested action: what to do
and why, the steps, and a brief for each kind of doer, complete enough to hand over as it
is:

  ai_agent   an AI agent with a browser: what to open, check and prepare. It must stop and
             ask the owner before submitting anything official or paying anything.
  employee   a member of staff.
  external   an outside professional (a tax agent, the company secretary, a lawyer), formal.

The Upload page lists them (scripts/wiki_server.py, /api/actions): the owner picks who
does each one, hands over its brief, and marks it done or dismisses it. Nothing here
sends anything anywhere, and nothing is done by itself.

Stored like the review items (scripts/wiki_review.py): actions/open/<id>.json while it is
open, actions/done/<id>.json once done or dismissed; reopening moves it back.

An item:
  {"id": "AC-20261003-correct-tax-return",      the file's name, checked like a review id
   "created": "2026-10-03",
   "from": "RV-20261003-employee-tax-number",   the review item it came from, if any
   "title": "...",                               at most 160 characters
   "why": "...",                                 at most 1500
   "due": "2026-10-31" or "",                    only a date a document or the wiki gives
   "priority": "high" | "normal" | "low",
   "steps": ["...", ...],                        at most 12, each at most 300 characters
   "briefs": {"ai_agent": "...", "employee": "...", "external": "..."},   each at most 6000
   "assignee": "" | "ai_agent" | "employee" | "external",
   "sources": ["raw/general/x.pdf", "wiki/companies/y.md"],   at most 10, under raw/ or wiki/
   "status": "open" | "done" | "dismissed",      open/ holds "open"; done/ the other two
   "doneAt": "2026-10-03T10:00:00+08:00"}        when it was done or dismissed

Items are written by Claude, so every field is checked, never trusted (`normalise`):
lengths are clipped, bad paths and unknown keys dropped, and an item without a title or
without any brief is left out. With the model on this Mac, packets are applied by code and
no actions are made. Standard library only.
"""
import datetime
import json
import os
import re
import sys
import threading

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WIKI_DIR, "scripts"))
import wiki_events  # noqa: E402
import wiki_review  # noqa: E402

OPEN = os.path.join(WIKI_DIR, "actions", "open")
DONE = os.path.join(WIKI_DIR, "actions", "done")
LOCK_FILE = os.path.join(WIKI_DIR, ".wiki-engine", "state", "actions.lock")
LOCK = threading.Lock()
ID_RE = re.compile(r"^AC-[A-Za-z0-9][A-Za-z0-9-]{0,95}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ASSIGNEES = ("", "ai_agent", "employee", "external")
STATUSES = ("open", "done", "dismissed")
PRIORITIES = ("high", "normal", "low")
BRIEFS = ("ai_agent", "employee", "external")
WHO = {"ai_agent": "an AI agent", "employee": "an employee", "external": "an outside agent"}


def block(text, n):
    """Text that keeps its line breaks (a brief has steps and paragraphs), without control
    characters, at most n characters."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or (ch >= " " and ch != "\x7f"))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in text.split("\n"))).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def date_or_empty(v):
    v = str(v or "").strip()[:10]
    if not DATE_RE.match(v):
        return ""
    try:
        datetime.date.fromisoformat(v)
    except ValueError:
        return ""
    return v


def safe_source(p):
    """p if it is a plain path to something under raw/ or wiki/ that stays inside the wiki
    (a link out of it does not count), else ""."""
    if not isinstance(p, str):
        return ""
    p = p.strip()
    if not p or len(p) > 300 or "\\" in p or any(ch < " " or ch == "\x7f" for ch in p):
        return ""
    parts = p.split("/")
    if len(parts) < 2 or parts[0] not in ("raw", "wiki") or any(x in ("", ".", "..") for x in parts):
        return ""
    real = os.path.realpath(os.path.join(WIKI_DIR, *parts))
    base = os.path.realpath(os.path.join(WIKI_DIR, parts[0]))
    return p if real.startswith(base + os.sep) else ""


def normalise(raw, name, folder=OPEN):
    """An item as the Upload page needs it, or None. folder: where the file was found
    (open/ means "open"; done/ means done or dismissed)."""
    if not isinstance(raw, dict):
        return None
    iid = name[:-5] if name.endswith(".json") else name
    if not ID_RE.match(iid):
        return None
    title = wiki_review.clip(raw.get("title"), 160)
    briefs = {}
    if isinstance(raw.get("briefs"), dict):
        for k in BRIEFS:
            b = block(raw["briefs"].get(k), 6000)
            if b:
                briefs[k] = b
    if not title or not briefs:
        return None
    steps = []
    for s in raw.get("steps") if isinstance(raw.get("steps"), list) else []:
        s = wiki_review.clip(s, 300) if isinstance(s, str) else ""
        if s:
            steps.append(s)
        if len(steps) >= 12:
            break
    sources = []
    for p in raw.get("sources") if isinstance(raw.get("sources"), list) else []:
        p = safe_source(p)
        if p and p not in sources:
            sources.append(p)
        if len(sources) >= 10:
            break
    if folder == OPEN:
        status = "open"
    else:
        status = raw.get("status") if raw.get("status") in ("done", "dismissed") else "done"
    item = {
        "id": iid,
        "created": date_or_empty(raw.get("created")),
        "title": title,
        "why": block(raw.get("why"), 1500),
        "due": date_or_empty(raw.get("due")),
        "priority": raw.get("priority") if raw.get("priority") in PRIORITIES else "normal",
        "steps": steps,
        "briefs": briefs,
        "assignee": raw.get("assignee") if raw.get("assignee") in ASSIGNEES else "",
        "sources": sources,
        "status": status,
    }
    if isinstance(raw.get("from"), str) and wiki_review.ID_RE.match(raw["from"]):
        item["from"] = raw["from"]
    if status != "open" and isinstance(raw.get("doneAt"), str) and raw["doneAt"]:
        item["doneAt"] = raw["doneAt"][:40]
    if status != "open" and wiki_review.clip(raw.get("note"), 300):
        item["note"] = wiki_review.clip(raw.get("note"), 300)   # why it was settled, when the engine settled it
    return item


def load(folder, name):
    try:
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            return normalise(json.load(f), name, folder)
    except (OSError, ValueError):
        return None


def list_open():
    """Open actions: high priority first, then the nearest due date (none last), then the
    oldest. A file being written, or one that is not a valid item, is left out."""
    try:
        names = sorted(n for n in os.listdir(OPEN) if n.endswith(".json"))
    except OSError:
        return []
    out = [i for i in (load(OPEN, n) for n in names) if i]
    out.sort(key=lambda i: (PRIORITIES.index(i["priority"]), i["due"] or "9999", i["created"], i["id"]))
    return out


def list_done(limit=50):
    """Done and dismissed actions, the most recently settled first."""
    try:
        names = [n for n in os.listdir(DONE) if n.endswith(".json")]
    except OSError:
        return []
    names.sort(key=lambda n: wiki_review.mtime(os.path.join(DONE, n)), reverse=True)
    return [i for i in (load(DONE, n) for n in names[:limit]) if i]


def count_open():
    """The open actions the Upload page lists (a file that is not a valid item never counts:
    actions are few, so they are read rather than counted by name)."""
    return len(list_open())


def revision():
    """Changes whenever an action is added, assigned, done or reopened (for the Upload page)."""
    marks = []
    for d in (OPEN, DONE):
        try:
            marks.append(str(os.stat(d).st_mtime_ns))
        except OSError:
            marks.append("0")
    return "-".join(marks)


def dismiss_from(review_id, note):
    """Dismiss every open action drafted from this review item (its answer was changed):
    each moves to done/ as dismissed, with the note saying why. Returns how many."""
    if not wiki_review.ID_RE.match(review_id or ""):
        return 0
    try:
        names = sorted(n for n in os.listdir(OPEN) if n.endswith(".json") and ID_RE.match(n[:-5]))
    except OSError:
        return 0
    n = 0
    with wiki_review.changing(LOCK_FILE, LOCK):
        for name in names:
            item = load(OPEN, name)
            if not item or item.get("from") != review_id:
                continue
            item.update(status="dismissed", doneAt=wiki_review.stamp(), note=wiki_review.clip(note, 300))
            wiki_review.write_json(os.path.join(DONE, name), item)
            try:
                os.unlink(os.path.join(OPEN, name))
            except OSError:
                pass
            n += 1
            wiki_events.emit("action-updated", id=item["id"], title=item["title"], assignee=item["assignee"],
                             status="dismissed", msg="Dismissed: " + note)
    return n


def update(iid, assignee=None, status=None):
    """Assign an action, or settle it: "done" and "dismissed" move it to done/ with the time,
    "open" moves it back. Returns {"ok", "message"}, with "missing": True when there is no
    such action. Values outside ASSIGNEES and STATUSES raise ValueError."""
    if assignee is not None and assignee not in ASSIGNEES:
        raise ValueError(f"assignee {assignee!r}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"status {status!r}")
    missing = {"ok": False, "message": "That action was not found; it may have been settled meanwhile.",
               "missing": True}
    if not ID_RE.match(iid or ""):
        return missing
    name = iid + ".json"
    with wiki_review.changing(LOCK_FILE, LOCK):
        folder = next((d for d in (OPEN, DONE) if os.path.isfile(os.path.join(d, name))), None)
        item = load(folder, name) if folder else None
        if not item:
            return missing
        said = []
        if assignee is not None and assignee != item["assignee"]:
            item["assignee"] = assignee
            said.append(f"Assigned to {WHO[assignee]}." if assignee else "No longer assigned.")
        if status is not None and status != item["status"]:
            item["status"] = status
            if status == "open":
                item.pop("doneAt", None)
                item.pop("note", None)
                said.append("It is back in the open list.")
            else:
                item["doneAt"] = wiki_review.stamp()
                said.append("Marked done." if status == "done" else "Dismissed.")
        if not said:
            return {"ok": True, "message": "Nothing changed."}
        dest = OPEN if item["status"] == "open" else DONE
        wiki_review.write_json(os.path.join(dest, name), item)
        if dest != folder:
            try:
                os.unlink(os.path.join(folder, name))
            except OSError:
                pass
    wiki_events.emit("action-updated", id=iid, title=item["title"], assignee=item["assignee"],
                     status=item["status"], msg=" ".join(said))
    return {"ok": True, "message": " ".join(said)}
