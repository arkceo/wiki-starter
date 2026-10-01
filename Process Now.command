#!/bin/bash
# Process Now — fold everything waiting in the queue (raw/_intake, raw/inbox) into the wiki
# right away, instead of waiting for the background runner. Safe to run any time: if the
# runner is already busy, this says so and stops.
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
echo "Processing the queue now. This can take a few minutes per document."
echo
bash scripts/wiki_runner.sh
echo
echo "Finished. Full log: ~/Library/Logs/wiki-starter/runner.log"
[ -t 0 ] && read -r -p "Press Return to close. " _
exit 0
