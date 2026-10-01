#!/usr/bin/env python3
"""lint_frontmatter.py — keep wiki frontmatter YAML-safe, forever.

An unquoted `: ` (or a leading YAML special) inside a frontmatter value kills
gray-matter/js-yaml in the Quartz build and takes the whole wiki site down
(seen 2026-07-11: an engine-written `evidence:` value containing
"(Open questions: 21 ...)" broke frontmatter.ts:69).

This linter walks wiki/**/*.md (and generated/**/*.md), and for every simple
`key: value` frontmatter line whose value is unquoted and dangerous — contains
`: `, ends with `:`, starts with a YAML special character, or contains ` #` —
it wraps the value in double quotes (escaping inner quotes). Multi-line/indented
lines and already-quoted values are left alone.

Usage:
    python3 scripts/lint_frontmatter.py            # fix in place, report
    python3 scripts/lint_frontmatter.py --check    # report only, exit 1 if dirty
Pure stdlib. Run by ingest/intake/refresh before their commit step, and safe to
run any time locally.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("wiki", "generated")
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t](.*)$")
SPECIALS = ("&", "*", "!", "|", ">", "%", "@", "`")


def dangerous(value):
    v = value.strip()
    if not v:
        return False
    if v[0] == '"' and v.endswith('"') and len(v) > 1:
        return False                                   # already quoted
    if v[0] == "'" and v.endswith("'") and len(v) > 1:
        return False
    if v[0] in ("[", "{"):
        return False                                   # legal YAML flow collection — leave alone
    if ": " in v or v.endswith(":"):
        return True                                    # nested-mapping ambiguity
    if v[0] in SPECIALS:
        return True                                    # YAML special lead char
    if " #" in v:
        return True                                    # inline-comment ambiguity
    return False


def fix_text(text):
    if not text.startswith("---\n"):
        return text, 0
    end = text.find("\n---", 4)
    if end < 0:
        return text, 0
    head = text[4:end]
    fixed, n = [], 0
    for ln in head.split("\n"):
        m = KEY_RE.match(ln)
        if m and dangerous(m.group(2)):
            v = m.group(2).strip().replace('"', '\\"')
            fixed.append('%s: "%s"' % (m.group(1), v))
            n += 1
        else:
            fixed.append(ln)
    if not n:
        return text, 0
    return "---\n" + "\n".join(fixed) + text[end:], n


def main():
    check = "--check" in sys.argv
    dirty = []
    for base in SCAN_DIRS:
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, base)):
            for name in files:
                if not name.endswith(".md"):
                    continue
                p = os.path.join(dirpath, name)
                try:
                    with open(p, encoding="utf-8") as f:
                        text = f.read()
                except (OSError, UnicodeDecodeError):
                    continue
                new, n = fix_text(text)
                if n:
                    rel = os.path.relpath(p, ROOT)
                    dirty.append((rel, n))
                    if not check:
                        with open(p, "w", encoding="utf-8") as f:
                            f.write(new)
    for rel, n in dirty:
        print("%s: %s (%d value%s quoted)" %
              ("DIRTY" if check else "FIXED", rel, n, "s" if n > 1 else ""))
    if not dirty:
        print("frontmatter clean")
    sys.exit(1 if (check and dirty) else 0)


if __name__ == "__main__":
    main()
