#!/usr/bin/env python3
"""wiki_settings.py — the reading settings, and the three performance modes that set them.

The owner picks a mode in Settings ("performance" in wiki.config.json); the mode decides
three figures, tuned for each way of reading:

                          documents per batch      largest upload   spending cap per batch
                          fast / classic / local
  economical                 20 /  5 / 10              100 MB             US$2
  moderate                   40 /  8 / 20              500 MB             US$5
  maximum (the default)     100 / 10 / 40             2048 MB             US$25

  - Documents per batch. Fast reading (Claude, the default) reads a batch many documents at
    once and then writes the overviews of every page the batch touched: bigger batches
    mean fewer pauses and fewer overviews written again, so they finish sooner and cost a
    little less. Classic reading gives one Claude session a batch, and a session has a turn
    cap, so its batches stay small. The model on this Mac starts once per batch.
  - Largest upload: the biggest single file the Upload page takes.
  - Spending cap per batch: no new Claude call starts once a batch has cost this much
    (Claude's own figures); the documents it did not reach wait for the next run. A cap
    is a ceiling, not a price: a document costs the same in every mode.

Reading speed ("parallelBatches", 1 to 6, default 6: the fastest) is set apart from the
mode: fast reading reads 3 documents at once per step (up to 18), classic reading runs
that many sessions at once. Speed changes how soon an upload is done, not what it costs.

A figure set by hand in wiki.config.json ("docsPerBatch", "fast.docsPerBatch",
"maxUploadMb", "maxSpendPerBatchUsd") wins over the mode's; Settings then shows "Custom".

Wikis from before modes are moved over once ("settingsVersion": 2): figures still at the
old defaults (8 per classic batch, 20 for the model on this Mac, 40 for fast reading,
500 MB, US$5, 3 batches at once) are dropped, so the new defaults (Maximum, fastest)
apply; figures the owner changed are kept. The review confidence bar, which no longer has
a control, goes back to 70%.

Usage (the runner):
  wiki_settings.py get KEY     docsPerBatch | maxUploadMb | maxSpendPerBatchUsd | parallelBatches
  wiki_settings.py migrate     the one-off move to modes (does nothing once done)
Standard library only.
"""
import json
import math
import os
import secrets
import sys

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(WIKI_DIR, "wiki.config.json")

MODES = ("economical", "moderate", "maximum")
DEFAULT_MODE = "maximum"
SPEED_MAX = 6
DEFAULT_SPEED = SPEED_MAX
VERSION = 2
# mode -> reading -> figures. "docs": documents per batch, "mb": largest upload, "usd": cap.
PRESETS = {
    "economical": {"fast": 20, "classic": 5, "local": 10, "mb": 100, "usd": 2.0},
    "moderate": {"fast": 40, "classic": 8, "local": 20, "mb": 500, "usd": 5.0},
    "maximum": {"fast": 100, "classic": 10, "local": 40, "mb": 2048, "usd": 25.0},
}
LIMITS = {"docsPerBatch": (1, 200), "maxUploadMb": (1, 4096), "maxSpendPerBatchUsd": (0.5, 100.0),
          "parallelBatches": (1, SPEED_MAX)}
# The defaults before modes: a figure still at one of these was never chosen by the owner.
OLD_DEFAULTS = {"docsPerBatch": (8, 20), "fast.docsPerBatch": (40,), "maxUploadMb": (500,),
                "maxSpendPerBatchUsd": (5,), "maxSpendPerRunUsd": (5,), "parallelBatches": (3,)}


def load(path=CONFIG):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save(cfg, path=CONFIG):
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def reading(cfg):
    """How documents are read: "local" (the model on this Mac), "classic" or "fast"."""
    if (cfg.get("engine") or "claude") == "local":
        return "local"
    return "classic" if cfg.get("pipeline") == "classic" else "fast"


def mode(cfg):
    m = cfg.get("performance")
    return m if m in MODES else DEFAULT_MODE


def _num(v, kind):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None   # json.load turns 1e999, Infinity and NaN into floats like these
    try:
        n = kind(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if isinstance(n, int) or math.isfinite(n) else None


def _fast(cfg):
    return cfg.get("fast") if isinstance(cfg.get("fast"), dict) else {}


def _explicit(cfg, key):
    """The figure set by hand for this wiki's way of reading, or None."""
    if key == "docsPerBatch":
        v = _fast(cfg).get("docsPerBatch") if reading(cfg) == "fast" else cfg.get("docsPerBatch")
        v = _num(v, int)
    elif key == "maxSpendPerBatchUsd":
        v = _num(cfg.get("maxSpendPerBatchUsd", cfg.get("maxSpendPerRunUsd")), float)
    else:
        v = _num(cfg.get(key), int)
    if v is None:
        return None
    lo, hi = LIMITS[key]
    return v if lo <= v <= hi else None


def preset(name, rd):
    p = PRESETS[name]
    return {"docsPerBatch": p[rd], "maxUploadMb": p["mb"], "maxSpendPerBatchUsd": p["usd"]}


def effective(cfg):
    """The figures a run uses: those set by hand, else the mode's; and the reading speed."""
    out = preset(mode(cfg), reading(cfg))
    for k in out:
        v = _explicit(cfg, k)
        if v is not None:
            out[k] = v
    speed = _explicit(cfg, "parallelBatches")
    out["parallelBatches"] = speed if speed is not None else DEFAULT_SPEED
    return out


def shown_mode(cfg):
    """The mode Settings shows: the chosen one, or "custom" when figures set by hand differ."""
    eff = effective(cfg)
    want = preset(mode(cfg), reading(cfg))
    return mode(cfg) if all(eff[k] == want[k] for k in want) else "custom"


def presets_for(cfg):
    rd = reading(cfg)
    return {m: preset(m, rd) for m in MODES}


def apply_mode(cfg, name):
    """Choose a mode: its figures apply to every way of reading, so hand-set ones go."""
    if name not in MODES:
        raise ValueError(name)
    cfg["performance"] = name
    for k in ("docsPerBatch", "maxUploadMb", "maxSpendPerBatchUsd", "maxSpendPerRunUsd"):
        cfg.pop(k, None)
    fast = dict(_fast(cfg))
    if fast.pop("docsPerBatch", None) is not None:
        if fast:
            cfg["fast"] = fast
        else:
            cfg.pop("fast", None)
    return cfg


def _old_default(cfg, key):
    if key == "fast.docsPerBatch":
        v = _fast(cfg).get("docsPerBatch")
    else:
        v = cfg.get(key)
    olds = OLD_DEFAULTS[key]
    if key == "docsPerBatch":   # 8 was classic reading's default, 20 the local model's
        olds = {"local": (20,), "classic": (8,)}.get(reading(cfg), olds)
    return v is None or (_num(v, float) is not None and float(v) in olds)


def migrate(cfg):
    """The one-off move to modes. Returns True if the config changed."""
    if cfg.get("settingsVersion") == VERSION:
        return False
    if "performance" not in cfg:
        keys = ("docsPerBatch", "fast.docsPerBatch", "maxUploadMb", "maxSpendPerBatchUsd", "maxSpendPerRunUsd")
        if all(_old_default(cfg, k) for k in keys):
            apply_mode(cfg, DEFAULT_MODE)
        else:
            # The owner changed some figures: keep every figure as it was, the ones never
            # set at their old defaults, so nothing about this wiki's runs changes.
            if "maxSpendPerBatchUsd" not in cfg and "maxSpendPerRunUsd" not in cfg:
                cfg["maxSpendPerBatchUsd"] = 5
            cfg.setdefault("maxUploadMb", 500)
            cfg.setdefault("docsPerBatch", 20 if reading(cfg) == "local" else 8)
            if _fast(cfg).get("docsPerBatch") is None:
                cfg["fast"] = dict(_fast(cfg), docsPerBatch=40)
    if "parallelBatches" in cfg and _old_default(cfg, "parallelBatches"):
        del cfg["parallelBatches"]
    # The confidence bar has no control any more: Jev answers when it is at least 70% sure.
    r = cfg.get("review")
    if isinstance(r, dict) and r.get("autoConfidence", 0.7) != 0.7:
        cfg["review"] = dict(r, autoConfidence=0.7)
    cfg["settingsVersion"] = VERSION
    return True


def _fmt(v):
    return f"{v:g}" if isinstance(v, float) else str(v)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "get" and argv[1] in ("docsPerBatch", "maxUploadMb",
                                                           "maxSpendPerBatchUsd", "parallelBatches"):
        print(_fmt(effective(load())[argv[1]]))
        return 0
    if argv == ["migrate"]:
        cfg = load()
        if cfg and migrate(cfg):
            save(cfg)
        return 0
    print("usage: wiki_settings.py get KEY | migrate", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
