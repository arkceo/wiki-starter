#!/bin/bash
# Open Wiki — open the wiki in the default browser. It is served only to this Mac.
cd "$(dirname "$0")" || exit 1
port=$(sed -n 's/.*"port"[^0-9]*\([0-9][0-9]*\).*/\1/p' wiki.config.json 2>/dev/null | head -1)
open "http://127.0.0.1:${port:-8765}/"
