#!/usr/bin/env python3
"""wiki_jev.py — Jev, TypeSafe's decision model, as the Review queue's judge.

Jev writes nothing. It is given a state (text) and multiple-choice questions, and returns
the option it picks, a probability for every option and a confidence. Here the questions
are the open review items (scripts/wiki_review.py), whose options were written by Claude
or by the runner, and the state is what the wiki and the document say about each one.

  - Batches: every open question about the same document goes in one request, with that
    document's excerpts as the state (up to 64 questions a request). Requests for
    different documents are sent at the same time.
  - Settled or not: with each question Jev is also asked, yes or no, whether the documents
    settle it, or whether it needs the owner's own knowledge or decision. Measured on real
    questions: plain facts scored 0.95-0.97, judgment calls 0.09-0.20 - while Jev's
    confidence in its pick was as high as 0.95 on a judgment call it got wrong ("which
    company is ours?"). Confidence alone is not a safe trigger.
  - Auto-review ("review": {"mode": "auto"} in wiki.config.json): an answer is applied as
    if the owner had clicked it only when the documents settle the question (SETTLED) and
    Jev's confidence is at least "autoConfidence" (default 0.7). A file the runner set
    aside is the exception: its options are the runner's own and all reversible, so only
    the confidence counts. Anything else, or an option marked not automatic, waits.
  - Only with Claude: a wiki that reads with the model on this Mac sends nothing anywhere,
    so it never calls Jev.

What leaves the Mac, and only when a key is saved: each question, its options, and the
excerpts of the wiki pages and the document it is about. The key lives in the macOS
Keychain (service "wiki-starter", account "typesafe-api-key"), never in a file.

Usage: wiki_jev.py score     score the open items now (and apply them in auto mode)
Standard library only.
"""
import concurrent.futures
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WIKI_DIR, "scripts"))
import wiki_events  # noqa: E402
import wiki_review  # noqa: E402

API_URL = os.environ.get("WIKI_JEV_URL") or "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"            # pinned: the auto-review threshold was chosen against it
KEYCHAIN_SERVICE = "wiki-starter"
KEYCHAIN_ACCOUNT = "typesafe-api-key"
SECURITY = os.environ.get("WIKI_SECURITY_BIN") or "security"
STATE_CHARS = 60000             # about 15k tokens; Jev takes 32k for state and question
PAGE_CHARS = 9000
DOC_CHARS = 12000
MAX_QUESTIONS = 64              # per request; each item asks two (its choice, and SETTLE)
SETTLED = 0.8
SETTLE = ("Can this question be settled from the state alone, by what the documents and wiki "
          "pages state, without the owner's own knowledge, intent or business decision?")
PARALLEL = 4
RETRY_AFTER = 600               # an item Jev could not score is tried again after this


class JevError(Exception):
    pass


# --------------------------------------------------------------------- key -------
def get_key():
    if os.environ.get("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"].strip()
    try:
        r = subprocess.run([SECURITY, "find-generic-password", "-s", KEYCHAIN_SERVICE,
                            "-a", KEYCHAIN_ACCOUNT, "-w"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def set_key(key):
    r = subprocess.run([SECURITY, "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                        "-a", KEYCHAIN_ACCOUNT, "-l", "Wiki (TypeSafe Jev API key)", "-w", key],
                       capture_output=True, timeout=20)
    return r.returncode == 0


def delete_key():
    subprocess.run([SECURITY, "delete-generic-password", "-s", KEYCHAIN_SERVICE,
                    "-a", KEYCHAIN_ACCOUNT], capture_output=True, timeout=20)


# ---------------------------------------------------------------- settings -------
def load_config():
    try:
        with open(os.path.join(WIKI_DIR, "wiki.config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
            return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def review_settings(cfg=None):
    cfg = load_config() if cfg is None else cfg
    r = cfg.get("review") if isinstance(cfg.get("review"), dict) else {}
    mode = r.get("mode") if r.get("mode") in ("auto", "manual") else "manual"
    try:
        bar = min(0.99, max(0.5, float(r.get("autoConfidence", 0.7))))
    except (TypeError, ValueError):
        bar = 0.7
    return {"mode": mode, "autoConfidence": bar}


def available(cfg=None):
    """Jev is used only when Claude reads the documents (they already leave the Mac)."""
    cfg = load_config() if cfg is None else cfg
    return (cfg.get("engine") or "claude") != "local"


# -------------------------------------------------------------------- call -------
def call(state, questions, key, timeout=30):
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "User-Agent": "wiki-starter"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except OSError:
            pass
        why = {401: "TypeSafe did not accept the API key", 403: "TypeSafe did not accept the API key",
               402: "the TypeSafe account has no credit left", 429: "TypeSafe asked to slow down"}.get(e.code)
        raise JevError(why or f"TypeSafe answered {e.code} {detail}".strip())
    except (urllib.error.URLError, OSError) as e:
        raise JevError(f"could not reach TypeSafe ({getattr(e, 'reason', e)})")
    except ValueError:
        raise JevError("TypeSafe sent an answer that is not JSON")
    if not isinstance(out, dict) or not isinstance(out.get("answers"), dict):
        raise JevError("TypeSafe sent an answer without answers")
    return out


def test_key(key):
    """(ok, message) for a key, with one tiny question."""
    try:
        out = call("A wiki checks that its review judge is reachable.",
                   {"ready": {"type": "choice", "instructions": "Is this a test?",
                              "criteria": {"yes": "It is a test", "no": "It is not a test"}}}, key, timeout=20)
    except JevError as e:
        return False, str(e)
    return True, f"Jev answered ({out.get('model') or MODEL})."


# ------------------------------------------------------------------- state -------
def read_text(rel_path, limit):
    """A wiki page or a text document, never anything else in the folder (the names come
    from review items, which Claude writes)."""
    if not rel_path.startswith(("wiki/", "raw/")) or not rel_path.endswith((".md", ".txt", ".csv")):
        return ""
    path = os.path.join(WIKI_DIR, rel_path)
    real, base = os.path.realpath(path), os.path.realpath(WIKI_DIR)
    if not real.startswith(base + os.sep) or not os.path.isfile(real):
        return ""
    try:
        with open(real, encoding="utf-8", errors="replace") as f:
            text = f.read(limit + 1)
    except OSError:
        return ""
    return text if len(text) <= limit else text[:limit] + "\n[…]"


def document_excerpt(path):
    text = wiki_review.mirror_of(path) if path else ""
    if not text and path:
        text = read_text(path, DOC_CHARS)
    _, body = wiki_review.split_mirror(text)
    return body[:DOC_CHARS] + ("\n[…]" if len(body) > DOC_CHARS else "")


def build_state(items):
    """What the wiki and the document say, for questions about one document: the review
    notes first (the conflict itself), then the pages concerned, then the document."""
    first = items[0]
    out = [f"Review questions about one document in a company's wiki: {first['file'] or 'no single document'}."]
    for it in items:
        if it.get("context"):
            out.append(f"Review note ({it['id']}): {it['context']}")
        if isinstance(it.get("facts"), dict):
            out.append("Facts about the file: " + json.dumps(it["facts"], ensure_ascii=False))
    seen = set()
    for it in items:
        for p in it["pages"]:
            if p in seen or len(seen) >= 6:
                continue
            seen.add(p)
            text = read_text(p, PAGE_CHARS)
            if text:
                out.append(f"Wiki page {p}:\n{text}")
    doc = document_excerpt(first["file"])
    if doc:
        out.append(f"The document {first['file']} (converted to text):\n{doc}")
    state = "\n\n".join(out)
    return state[:STATE_CHARS]


def question_for(item):
    criteria = {}
    for o in item["options"]:
        criteria[o["key"]] = o["label"] + (f". If chosen: {o['effect']}" if o.get("effect") else "")
    return {"type": "choice",
            "instructions": (f"{item['question']} Choose the answer the state supports best: what "
                             "the documents and the wiki pages say directly. When two sources "
                             "disagree, weigh which is more recent and more formal."),
            "criteria": criteria}


def groups_of(items):
    by_file = {}
    for it in items:
        by_file.setdefault(it["file"] or it["id"], []).append(it)
    out = []
    for group in by_file.values():
        for i in range(0, len(group), MAX_QUESTIONS // 2):
            out.append(group[i:i + MAX_QUESTIONS // 2])
    return out


def score_group(group, key):
    names = {f"q{i + 1}": it for i, it in enumerate(group)}
    questions = {}
    for n, it in names.items():
        questions[n] = question_for(it)
        questions["s" + n[1:]] = {"type": "noul", "instructions": f"{it['question']} {SETTLE}"}
    out = call(build_state(group), questions, key)
    model, results = out.get("model") or MODEL, {}
    for n, it in names.items():
        a = out["answers"].get(n)
        settled = (out["answers"].get("s" + n[1:]) or {}).get("noul")
        keys = {o["key"] for o in it["options"]}
        if not isinstance(a, dict) or a.get("choice") not in keys:
            results[it["id"]] = {"error": "Jev gave no usable answer", "at": time.time()}
            continue
        probs = {k: round(float(v), 4) for k, v in (a.get("probabilities") or {}).items()
                 if k in keys and isinstance(v, (int, float))}
        conf = a.get("confidence")
        if not isinstance(conf, (int, float)):
            conf = probs.get(a["choice"], 0.0)
        results[it["id"]] = {"model": model, "choice": a["choice"], "probabilities": probs,
                             "confidence": round(float(conf), 4), "at": time.time(),
                             "settled": round(float(settled), 4) if isinstance(settled, (int, float)) else None}
    return results


def score(items, key):
    """Score items, all groups at once. Returns {id: jev result or {"error": ...}}."""
    results = {}
    groups = groups_of(items)
    with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        futures = {pool.submit(score_group, g, key): g for g in groups}
        for fut in concurrent.futures.as_completed(futures):
            try:
                results.update(fut.result())
            except JevError as e:
                for it in futures[fut]:
                    results[it["id"]] = {"error": str(e), "at": time.time()}
    return results


# -------------------------------------------------------------------- run -------
def needs_score(item, force=False):
    j = item.get("jev")
    if force or not isinstance(j, dict):
        return True
    if "error" not in j and "settled" not in j:
        return True   # scored before the settled question existed
    return "error" in j and time.time() - float(j.get("at") or 0) > RETRY_AFTER


def settled(item):
    """Whether the documents settle the question (a set-aside file always counts)."""
    if item["kind"] == "unreadable":
        return True
    return float((item.get("jev") or {}).get("settled") or 0) >= SETTLED


def auto_answer(item, bar):
    """The option auto-review applies, or None."""
    j = item.get("jev") or {}
    if "error" in j or float(j.get("confidence") or 0) < bar or not settled(item):
        return None
    opt = next((o for o in item["options"] if o["key"] == j.get("choice")), None)
    return opt if opt and opt.get("auto") is not False else None


def run(force_ids=(), key=None):
    """One pass: score what needs it, then apply in auto mode. Returns a summary."""
    cfg = load_config()
    summary = {"scored": 0, "applied": 0, "error": "", "queued": False}
    if not available(cfg):
        return summary
    items = wiki_review.list_open()
    todo = [it for it in items if needs_score(it, it["id"] in force_ids)]
    if todo:
        key = key if key is not None else get_key()
        if key:
            results = score(todo, key)
            for it in todo:
                if it["id"] in results:
                    it["jev"] = results[it["id"]]
                    wiki_review.save_open(it)
            ok = [r for r in results.values() if "error" not in r]
            errors = sorted({r["error"] for r in results.values() if "error" in r})
            summary["scored"] = len(ok)
            summary["error"] = "; ".join(errors)
            if ok:
                wiki_events.emit("review-scored", count=len(ok))
            if errors:
                wiki_events.emit("review-score-failed", msg=summary["error"])
    rs = review_settings(cfg)
    if rs["mode"] == "auto":
        for it in wiki_review.list_open():
            opt = auto_answer(it, rs["autoConfidence"])
            if not opt:
                continue
            res = wiki_review.answer(it["id"], key=opt["key"], by="jev")
            if res.get("ok"):
                summary["applied"] += 1
                summary["queued"] = summary["queued"] or res.get("queued", False)
    return summary


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["score"]:
        print(__doc__.strip().splitlines()[-2])
        return 2
    print(json.dumps(run(force_ids=set(argv[1:]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
