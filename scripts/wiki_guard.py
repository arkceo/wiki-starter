#!/usr/bin/env python3
"""wiki_guard.py — a Claude Code hook the runner adds while several sessions file at once.

Claude's Write tool replaces a file whole. When two sessions read different documents
about the same company at the same moment, both can find its page missing and both
create it; the second Write silently replaces the first session's page. Edit is safe (it
changes only the text it names), so while sessions overlap, Write may only create a file
that does not exist yet. Measured: two real sessions created the same company page 13
seconds apart, and the second replaced the first.

Run by Claude Code before each Write (PreToolUse), with the call as JSON on stdin. Exit 2
refuses the call and tells Claude why; exit 0 lets it through. Standard library only.
"""
import json
import os
import sys


def main():
    try:
        call = json.load(sys.stdin)
    except ValueError:
        return 0
    if call.get("tool_name") != "Write":
        return 0
    path = (call.get("tool_input") or {}).get("file_path") or ""
    if path and not os.path.isabs(path):
        path = os.path.join(call.get("cwd") or os.getcwd(), path)
    if path and os.path.exists(path):
        print(f"{os.path.basename(path)} already exists: another session filing at the same time may "
              "have just created it. Read it, then add to it with Edit; never replace it with Write.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
