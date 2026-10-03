#!/usr/bin/env python3
"""wiki_estimate.py — how long the documents still waiting will take, and what they cost.

Measured, never guessed: every batch the runner reads leaves a `batch` event when it
starts and a `claude-end` event when it ends, with Claude's reported cost. From the most
recent batches that finished cleanly (from this run or earlier ones with the same
engine) this works out the time and cost per document, then plays the rest of the queue
through the same lanes the runner uses (the first batch of a run alone, then
parallelBatches at once), with the pause measured between one batch ending and the next
starting, and the time the last run took to finish after its last batch (saving, the
site build). No figure is shown before MIN_BATCHES batches have been measured.

Cost is what Claude reports for each session (`total_cost_usd`). With an API key that is
the bill; with a Claude plan it is the same work valued at API prices, which the plan
covers within its usage limits. The model on this Mac costs nothing.

The viewer keeps one Tracker and feeds it the event log as it grows, so a poll reads only
the new lines. Standard library only.

Usage: wiki_estimate.py            the current estimate as JSON (for checks)
"""
import collections
import datetime
import heapq
import json
import math
import os
import sys
import threading

import wiki_settings

MIN_BATCHES = 2      # batches measured before any figure is shown
WINDOW = 10          # the most recent clean batches the rates come from
SLOW_SHARE = 0.15    # a batch past its expected time still has this share of it to go


def parse_t(s):
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return None


def number(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v >= 0 else None


class Tracker:
    """Folds events into what an estimate needs: the current run's batches and spending,
    and the recent finished batches per engine."""

    def __init__(self):
        self.lock = threading.Lock()
        self.run = None          # the run in progress, or the last one
        self.history = {"claude": collections.deque(maxlen=WINDOW),   # classic batches
                        "fast": collections.deque(maxlen=WINDOW),     # the fast pipeline's
                        "local": collections.deque(maxlen=WINDOW)}
        self.build_seconds = None
        self._build_started = None
        self.gaps = collections.deque(maxlen=20)   # a batch ending -> the next one starting
        self.tail = None                           # the last batch ending -> the run ending
        self.auth = ""
        self._files = {}         # path -> (inode, offset)

    # ------------------------------------------------------------------ events
    def feed(self, rec):
        kind, t = rec.get("type"), parse_t(rec.get("t"))
        if t is None:
            return
        if kind == "run-start":
            self.auth = rec.get("auth") or self.auth
            self.run = {"id": rec.get("run") or "", "start": t, "end": None,
                        "docs": int(number(rec.get("docs")) or 0), "spent": 0.0, "costs": 0,
                        "batches": {}, "filed": set(), "auth": rec.get("auth") or "", "last_end": None}
            return
        run = self.run
        if kind == "build-start":
            self._build_started = t
        elif kind in ("build-done", "build-failed") and self._build_started is not None:
            if kind == "build-done":
                self.build_seconds = max(0.0, t - self._build_started)
            self._build_started = None
        if run is None or (rec.get("run") and rec.get("run") != run["id"]):
            return
        if kind == "batch":
            k = int(number(rec.get("n")) or 0)
            run["batches"][k] = {"docs": int(number(rec.get("docs")) or 0), "start": t, "end": None}
            if run["last_end"] is not None and 0 <= t - run["last_end"] < 300:
                self.gaps.append(t - run["last_end"])
        elif kind == "claude-end":
            cost = number(rec.get("cost"))
            if cost is not None:
                run["spent"] += cost
                run["costs"] += 1
            label = rec.get("label") or ""
            b, engine = None, "claude"
            if label.startswith("intake#"):
                b = run["batches"].get(int(number(label[7:]) or 0))
            elif label in ("local", "fast"):   # one engine call per batch (the fast pipeline: Claude)
                engine = label
                open_ = [x for _, x in sorted(run["batches"].items()) if x["end"] is None]
                b = open_[0] if open_ else None
            if b is None or b["end"] is not None:
                return
            b["end"] = t
            run["last_end"] = max(run["last_end"] or t, t)
            if rec.get("ok") and b["docs"] > 0 and t > b["start"]:
                self.history[engine].append({"docs": b["docs"], "seconds": t - b["start"], "cost": cost})
        elif kind == "filed" and (rec.get("file") or "").startswith("raw/_intake/"):
            run["filed"].add(rec["file"])
        elif kind == "run-end":
            run["end"] = t
            if run["last_end"] is not None and t >= run["last_end"]:
                self.tail = t - run["last_end"]   # saving, the site build, packets

    def read(self, paths):
        """Feed the lines added to the log since the last call. `paths` lists the rotated
        log first, then the live one; after a rotation the old file is finished under its
        new name, so nothing is read twice or skipped."""
        with self.lock:
            for path in paths:
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                seen = None
                for p, (ino, off) in self._files.items():
                    if ino == st.st_ino:
                        seen = (p, off)
                start = seen[1] if seen else 0
                if seen and seen[0] != path:
                    del self._files[seen[0]]
                if st.st_size < start:
                    start = 0
                if st.st_size == start:
                    self._files[path] = (st.st_ino, start)
                    continue
                with open(path, "rb") as f:
                    f.seek(start)
                    data = f.read()
                end = data.rfind(b"\n") + 1     # a line still being written waits
                for line in data[:end].splitlines():
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        self.feed(rec)
                self._files[path] = (st.st_ino, start + end)

    # ------------------------------------------------------------------ estimate
    def estimate(self, waiting, engine, lanes, batch_size, running, now=None, fast=False):
        """waiting: documents in the intake queue now; running: whether the runner runs;
        fast: Claude reads with the fast pipeline (its batches are measured apart)."""
        with self.lock:
            return self._estimate(waiting, engine, lanes, batch_size, running, now, fast)

    def _estimate(self, waiting, engine, lanes, batch_size, running, now, fast=False):
        now = now if now is not None else datetime.datetime.now().timestamp()
        engine = "local" if engine == "local" else "claude"
        bucket = "fast" if fast and engine == "claude" else engine
        lanes = 1 if engine == "local" else min(max(1, int(number(lanes) or wiki_settings.DEFAULT_SPEED)),
                                                 wiki_settings.SPEED_MAX)
        batch_size = max(1, int(number(batch_size) or (20 if engine == "local" else 8)))
        hist = list(self.history[bucket])
        run = self.run if running and self.run and self.run["end"] is None else None
        out = {"engine": engine, "billing": "local" if engine == "local" else (self.auth or "login"),
               "measured": len(hist), "needed": MIN_BATCHES, "waiting": waiting, "batchSize": batch_size}
        if run:
            out["spent"] = round(run["spent"], 4) if run["costs"] else None
            out["runStart"] = run["start"]
        if not running and self.run and self.run["end"] is not None:
            last = self.run
            out["last"] = {"seconds": round(last["end"] - last["start"]), "docs": last["docs"],
                           "cost": round(last["spent"], 4) if last["costs"] else None}
        if len(hist) < MIN_BATCHES or not waiting:
            out["ready"] = False
            return out
        docs = sum(h["docs"] for h in hist)
        per_doc = sum(h["seconds"] for h in hist) / docs
        costed = [h for h in hist if h["cost"] is not None]
        cost_per_doc = (sum(h["cost"] for h in costed) / sum(h["docs"] for h in costed)) if costed else None

        # The batches being read now, and how long each still needs. Documents a running
        # batch has already filed have left the queue but are not paid for yet.
        busy, open_docs, first_alone, unpaid = [], 0, False, 0
        if run:
            paid = sum(b["docs"] for b in run["batches"].values() if b["end"] is not None)
            unpaid = max(len(run["filed"]) - paid, 0)
            open_ = [b for b in run["batches"].values() if b["end"] is None]
            first_alone = not any(b["end"] is not None for b in run["batches"].values())
            for b in open_:
                expect = b["docs"] * per_doc
                busy.append(max(expect - (now - b["start"]), SLOW_SHARE * expect))
                open_docs += b["docs"]
        rest = max(waiting + unpaid - open_docs, 0)
        gap = sum(self.gaps) / len(self.gaps) if self.gaps else 0.0
        queued = [batch_size * per_doc + gap] * (rest // batch_size)
        if rest % batch_size:
            queued.append((rest % batch_size) * per_doc + gap)
        if not busy and queued and not run:
            busy, queued, first_alone = [queued[0] - gap], queued[1:], True   # a new run: its first batch alone
        gate = min(busy) if first_alone and busy else 0.0
        heap = sorted(busy) + [gate] * max(lanes - len(busy), 0)
        heapq.heapify(heap)
        end = max(busy) if busy else 0.0
        for d in queued:
            t0 = max(heapq.heappop(heap), gate)
            heapq.heappush(heap, t0 + d)
            end = max(end, t0 + d)
        # After the last batch: what the last run took to finish (saving, the site build),
        # or, before any run has finished, the site builds seen so far.
        seconds = end + (self.tail if self.tail is not None else (self.build_seconds or 0.0))
        out.update(ready=True, seconds=round(seconds), finishAt=round(now + seconds),
                   perDocSeconds=round(per_doc, 1), lanes=lanes, basisDocs=docs,
                   perDocCost=round(cost_per_doc, 4) if cost_per_doc is not None else None)
        if cost_per_doc is not None:
            left = cost_per_doc * (waiting + unpaid)
            out["costLeft"] = round(left, 2)
            out["costTotal"] = round(left + (run["spent"] if run else 0.0), 2)
        return out


def main():
    wiki = os.environ.get("WIKI_DIR") or os.getcwd()
    state = os.path.join(wiki, ".wiki-engine", "state")
    cfg = {}
    try:
        cfg = json.load(open(os.path.join(wiki, "wiki.config.json"), encoding="utf-8"))
    except (OSError, ValueError):
        pass
    tr = Tracker()
    tr.read([os.path.join(state, "events.1.jsonl"), os.path.join(state, "events.jsonl")])
    waiting = 0
    for dirpath, _, files in os.walk(os.path.join(wiki, "raw", "_intake")):
        waiting += sum(1 for f in files if not f.startswith(".") and f != "README.md")
    running = os.path.isdir(os.path.join(state, "runner.lock"))
    cfg = cfg if isinstance(cfg, dict) else {}
    eff, fast = wiki_settings.effective(cfg), wiki_settings.reading(cfg) == "fast"   # the figures a run uses
    print(json.dumps(tr.estimate(waiting, cfg.get("engine"), 1 if fast else eff["parallelBatches"],
                                 eff["docsPerBatch"], running, fast=fast), indent=2))


if __name__ == "__main__":
    sys.exit(main())
