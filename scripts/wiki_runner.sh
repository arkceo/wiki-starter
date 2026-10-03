#!/bin/bash
# wiki_runner.sh — fold everything waiting in the queue into the wiki.
#
# The Upload page puts documents in raw/_intake/ and Update Packets in raw/inbox/. A
# LaunchAgent starts this script whenever something lands there, every 15 minutes as a
# fallback, and at login. It also runs from the Upload page's "Process now" button and
# from "Process Now.command". A run:
#
#   1. takes a single-writer lock (a second run exits at once);
#   2. waits until dropped files have finished copying;
#   3. routes misfiled drops (a PDF in the Inbox goes to Intake; a packet in Intake
#      goes to the Inbox);
#   4. reads new documents in batches (docsPerBatch in wiki.config.json: 8 with Claude,
#      20 with the model on this Mac): each batch is converted to Markdown
#      (scripts/to_markdown.py) and then filed by Claude (engine/prompts/intake.md)
#      before the next batch starts, so a large drop never outgrows one call's caps and
#      pages appear while it is still going;
#   5. has Claude apply Update Packets, five at a time (engine/prompts/ingest.md);
#   6. parks anything that failed twice in raw/_needs-review/ and says so in
#      wiki/_review.md, so a broken file never burns money every 15 minutes (only a
#      batch that actually ran counts as a try for its files), with a review item the
#      owner (or Jev) answers on the Upload page (scripts/wiki_review.py);
#   7. records newly filed documents in archive/ingestion-ledger.csv, tidies
#      frontmatter, refreshes the ingestion register;
#   8. commits the change to the folder's local git history (never pushed anywhere);
#   9. rebuilds the local site into public/ and shows a notification.
#
# Every step is also written to .wiki-engine/state/events.jsonl (scripts/wiki_events.py),
# which the Upload page shows as per-file progress and a live log, and what is happening
# at this moment to .wiki-engine/state/now.json, its live line.
#
# Claude runs with --permission-mode dontAsk and an explicit tool allowlist: it can read,
# write and edit files in this folder and use mv/mkdir/ls, nothing else. Every call (one
# batch) has a turn cap, a spending cap (maxSpendPerBatchUsd) and a wall-clock watchdog.
#
# With "engine": "local" in wiki.config.json, steps 4 and 5 are done instead by
# scripts/local_engine.py with a model running on this Mac, batch by batch, under a
# watchdog that stops it only when it shows no progress. Nothing leaves the Mac: no
# credentials are loaded, and Claude is never started.
#
# With Claude, unless "pipeline" is "classic", step 4 is done by the same script with Claude
# reading (scripts/local_engine.py --reader claude, scripts/wiki_claude.py): one call per
# document, many at once, and code that checks every answer against the document and
# writes the pages. Contradictions it finds become questions in the Review tab. Update
# Packets still go to a Claude session (step 5).
#
# Usage: wiki_runner.sh [--rebuild] [--no-settle]
#   --rebuild    only rebuild the site (used by the installer and the updater)
#   --no-settle  skip the wait-for-copies step (tests)
#
# Portable to macOS's bash 3.2 and BSD tools: no associative arrays, no GNU-only flags.

set -u

WIKI_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$WIKI_DIR" || exit 1

STATE_DIR="$WIKI_DIR/.wiki-engine/state"
LOG_DIR="${WIKI_LOG_DIR:-$HOME/Library/Logs/wiki-starter}"
LOG_FILE="$LOG_DIR/runner.log"
LOCK_DIR="$STATE_DIR/runner.lock"
ATTEMPTS_FILE="$STATE_DIR/attempts.txt"
LEDGER="archive/ingestion-ledger.csv"
KEYCHAIN_SERVICE="wiki-starter"

CLAUDE_BIN="${CLAUDE_BIN:-claude}"
PY="${WIKI_PYTHON:-$WIKI_DIR/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
NODE_BIN="${NODE_BIN:-node}"

SETTLE_SECONDS="${WIKI_SETTLE_SECONDS:-5}"
SETTLE_MAX_SECONDS="${WIKI_SETTLE_MAX_SECONDS:-600}"
MAX_INGEST_ROUNDS="${WIKI_MAX_INGEST_ROUNDS:-10}"
MAX_ATTEMPTS="${WIKI_MAX_ATTEMPTS:-2}"
CLAUDE_TIMEOUT_SECONDS="${WIKI_CLAUDE_TIMEOUT_SECONDS:-2400}"
LOCAL_TIMEOUT_SECONDS="${WIKI_LOCAL_TIMEOUT_SECONDS:-21600}"   # one batch, in all
LOCAL_STALL_SECONDS="${WIKI_LOCAL_STALL_SECONDS:-1200}"        # no sign of progress
ERROR_NOTIFY_EVERY_SECONDS="${WIKI_ERROR_NOTIFY_EVERY_SECONDS:-21600}"

ALLOWED_TOOLS="Read,Glob,Grep,Edit,Write,Bash(mv *),Bash(mkdir *),Bash(ls *)"

mkdir -p "$STATE_DIR" "$LOG_DIR"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"; [ -t 1 ] && printf '%s\n' "$*"; return 0; }

# One event for the Upload page: ev TYPE key=value ... (key:=json for numbers and lists).
ev() { "$PY" scripts/wiki_events.py emit "$@" >> "$LOG_FILE" 2>&1 || true; }

# What is happening right now, for the Upload page's live line: nowp PHASE key=value ...
nowp() { "$PY" scripts/wiki_events.py now "$@" >> "$LOG_FILE" 2>&1 || true; }

config_value() { # key default
  "$PY" - "$1" "$2" <<'PYEOF' 2>/dev/null || printf '%s' "$2"
import json, sys
key, default = sys.argv[1], sys.argv[2]
try:
    v = json.load(open("wiki.config.json"))
    for part in key.split("."):   # "fast.docsPerBatch": a key inside a section
        v = v.get(part) if isinstance(v, dict) else None
except Exception:
    v = None
print(default if v in (None, "") else v, end="")
PYEOF
}

notify() { # title message
  if [ -n "${WIKI_NOTIFY_CMD:-}" ]; then
    "$WIKI_NOTIFY_CMD" "$1" "$2"
  elif command -v osascript >/dev/null 2>&1; then
    osascript - "$1" "$2" >/dev/null 2>&1 <<'OSA'
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
OSA
  fi
  log "notify: $1 — $2"
}

notify_error() { # message; rate-limited so a broken setup does not alert every 15 minutes
  local stamp="$STATE_DIR/last-error-notify" now last=0
  ev error msg="$1"
  now=$(date +%s)
  [ -f "$stamp" ] && last=$(cat "$stamp" 2>/dev/null || echo 0)
  if [ $((now - last)) -ge "$ERROR_NOTIFY_EVERY_SECONDS" ]; then
    echo "$now" > "$stamp"
    notify "Wiki needs attention" "$1"
  else
    log "error (notification suppressed): $1"
  fi
}

# ---------------------------------------------------------------- lock --------
acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    date '+%H:%M' > "$LOCK_DIR/started"
    return 0
  fi
  local pid
  pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  log "removing stale lock (pid ${pid:-unknown})"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" 2>/dev/null || return 1
  echo $$ > "$LOCK_DIR/pid"
  date '+%H:%M' > "$LOCK_DIR/started"
}
release_lock() { rm -rf "$LOCK_DIR"; }
# A run that ends early takes its background work (reading lanes, conversion, a site
# rebuild) with it, so nothing outlives the lock.
kill_tree() { # pid: it and everything it started (conversion runs several processes deep)
  local c
  for c in $(pgrep -P "$1" 2>/dev/null); do kill_tree "$c"; done
  kill "$1" 2>/dev/null
}
stop_background() {
  local e p
  for e in ${running:-}; do
    p=${e%%:*}; kill_tree "$p"
  done
  for p in ${CONVERTER:-} ${BUILDER:-}; do kill_tree "$p"; done
}
# However a run ends, the live line must not keep describing a step that stopped.
finish_run() { stop_background; nowp idle; release_lock; }

# --------------------------------------------------------- credentials ---------
load_credentials() {
  [ -n "${ANTHROPIC_API_KEY:-}" ] && return 0
  [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && return 0
  command -v security >/dev/null 2>&1 || return 0
  local v
  v=$(security find-generic-password -s "$KEYCHAIN_SERVICE" -a claude-oauth-token -w 2>/dev/null || true)
  if [ -n "$v" ]; then export CLAUDE_CODE_OAUTH_TOKEN="$v"; return 0; fi
  v=$(security find-generic-password -s "$KEYCHAIN_SERVICE" -a anthropic-api-key -w 2>/dev/null || true)
  if [ -n "$v" ]; then export ANTHROPIC_API_KEY="$v"; return 0; fi
  # Neither stored: fall back to whatever login `claude` itself has.
  return 0
}

# ---------------------------------------------------------- migrations ---------
# Fixes an engine update needs. They live here, not in the updater, because the updater
# that runs an update is the old one; it calls this script with --rebuild at the end.
hash_of() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else sha256sum "$1" | cut -d' ' -f1; fi
}

# Earlier engines put Wiki Inbox and Wiki Intake links on the Desktop; the Upload page
# replaces them. Only from --rebuild (the updater and installer, run in Terminal): macOS
# guards the Desktop, and a background run reaching into it would raise a permission
# prompt. Only the names earlier engines used are looked at, only links into this wiki's
# queue are removed, and it is done once.
remove_old_desktop_links() {
  local done_flag="$STATE_DIR/desktop-links-removed" title name item target
  [ -f "$done_flag" ] && return 0
  title=$(config_value title Wiki)
  for name in "Wiki Inbox" "Wiki Intake" "$title Inbox" "$title Intake"; do
    item="$HOME/Desktop/$name"
    [ -L "$item" ] || continue
    target=$(readlink "$item")
    case "$target" in */raw/inbox|*/raw/_intake) ;; *) continue ;; esac
    [ "$(cd "$target" 2>/dev/null && pwd -P)" = "$WIKI_DIR/raw/${target##*/raw/}" ] || continue
    rm -f "$item" && log "removed the old Desktop link $item"
  done
  : > "$done_flag"
}

# Starter pages that explain how to use the wiki follow the engine while untouched:
# replaced by engine/seeds/<path> only if the copy here matches a hash an engine shipped
# (engine/seed-history.txt). A page anyone edited is never overwritten. Not committed
# here: the next run's commit records it, and `git revert HEAD` keeps undoing an update.
refresh_starter_pages() {
  local line h p
  [ -f engine/seed-history.txt ] || return 0
  while IFS= read -r line; do
    h="${line%%  *}"; p="${line#*  }"
    [ "$h" != "$line" ] && [ -f "$p" ] && [ -f "engine/seeds/$p" ] || continue
    [ "$(hash_of "$p")" = "$h" ] || continue
    cmp -s "engine/seeds/$p" "$p" && continue
    cp "engine/seeds/$p" "$p" && log "refreshed the starter page $p"
  done < engine/seed-history.txt
  return 0
}

# ------------------------------------------------------------- listing ---------
is_ignored_name() { # basename
  case "$1" in
    .*|Icon*|README.md|*.download|*.crdownload|*.part|*.partial|*.tmp|'~$'*) return 0 ;;
  esac
  return 1
}

list_pending() { # dir -> one path per line, sorted
  local d="$1" f
  [ -d "$d" ] || return 0
  find "$d" -type f 2>/dev/null | LC_ALL=C sort | while IFS= read -r f; do
    is_ignored_name "$(basename "$f")" && continue
    printf '%s\n' "$f"
  done
}

count_lines() { if [ -z "$1" ]; then echo 0; else printf '%s\n' "$1" | wc -l | tr -d ' '; fi; }

# Uploads in progress stream into .wiki-engine/state/uploads/ before they appear in the
# queue in one step, so they count as copying too.
partial_downloads_present() {
  find raw/inbox raw/_intake -type f \( -name '*.download' -o -name '*.crdownload' -o -name '*.part' -o -name '*.partial' \) 2>/dev/null | grep -q . && return 0
  # An upload is still arriving only if its file grew in the last few minutes; anything
  # older was left by a viewer that was stopped mid-upload.
  find .wiki-engine/state/uploads -type f -name '*.part' -mmin -5 2>/dev/null | grep -q .
}

drop_signature() { ls -lnR raw/inbox raw/_intake .wiki-engine/state/uploads 2>/dev/null | cksum; }

wait_for_settle() {
  local waited=0 a b
  a=$(drop_signature)
  while :; do
    sleep "$SETTLE_SECONDS"; waited=$((waited + SETTLE_SECONDS))
    b=$(drop_signature)
    if [ "$a" = "$b" ] && ! partial_downloads_present; then return 0; fi
    if [ "$waited" -ge "$SETTLE_MAX_SECONDS" ]; then
      log "files still changing after ${waited}s; processing what has settled"
      return 0
    fi
    a="$b"
  done
}

looks_like_packet() { # file
  head -c 400 "$1" 2>/dev/null | grep -q '^## \[[0-9]\{4\}-[0-9][0-9]-[0-9][0-9]\] *update *|'
}

route_to() { # file dest-dir
  local f="$1" d="$2" b
  b=$(basename "$f")
  mkdir -p "$d"
  if [ -e "$d/$b" ]; then
    # The same file dropped into both folders is processed once, not twice.
    if cmp -s "$f" "$d/$b"; then
      rm -f "$f" && log "dropped $f: an identical copy is already in $d"
      return 0
    fi
    b="$(date +%Y%m%d%H%M%S)-$b"
  fi
  mv "$f" "$d/$b" && log "routed $f -> $d/$b" && ev routed file="$f" to="$d/$b"
}

route_misfiled() {
  local f
  # A document dropped into the Inbox belongs in Intake.
  list_pending raw/inbox | while IFS= read -r f; do
    case "$f" in *.md|*.markdown) continue ;; esac
    route_to "$f" raw/_intake
  done
  # A packet dropped into Intake belongs in the Inbox.
  list_pending raw/_intake | while IFS= read -r f; do
    case "$f" in *.md) ;; *) continue ;; esac
    looks_like_packet "$f" || continue
    route_to "$f" raw/inbox
  done
}

list_packets() { list_pending raw/inbox | grep -E '\.(md|markdown)$' || true; }

snapshot_filed_sources() {
  find raw -type f \
    ! -path 'raw/inbox/*' ! -path 'raw/_intake/*' ! -path 'raw/_needs-review/*' \
    ! -name '.*' 2>/dev/null | LC_ALL=C sort
}

# ------------------------------------------------------------- claude ----------
# Claude's own verdict on a finished call: success, error_max_turns, ... ("" if none).
claude_outcome() { # stream-file
  "$PY" - "$1" <<'PYEOF' 2>/dev/null
import json, sys
outcome = ""
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        m = json.loads(line)
    except ValueError:
        continue
    if isinstance(m, dict) and m.get("type") == "result":
        outcome = m.get("subtype") or ""
print(outcome, end="")
PYEOF
}

# The file the live line says is being worked on (the one in progress when a step hung).
now_file() {
  "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("file", ""), end="")' \
    "$STATE_DIR/now.json" 2>/dev/null
}

# Claude Code settings adding the Write guard as a PreToolUse hook (JSON, paths quoted).
guard_settings() {
  "$PY" - "$PY" "$WIKI_DIR/scripts/wiki_guard.py" <<'PYEOF'
import json, shlex, sys
cmd = " ".join(shlex.quote(a) for a in sys.argv[1:3])
print(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": cmd}]}]}}))
PYEOF
}

# One Claude call. Returns 0 done; 3 stopped at its turn or spending cap (it ran, so its
# files count as tried); 4 stopped by the watchdog; 1 Claude could not run.
run_claude() { # label prompt-file max-turns [batch-file]
  local label="$1" prompt_file="$2" turns="$3" batch="${4:-}" budget pid fpid waited=0 rc outcome stopped=0
  local tag; tag=$(printf '%s' "$label" | tr -c 'A-Za-z0-9' '_')
  # Several sessions can run at once: each has its own stream, and its own record of
  # the file it is on (the shared live line may be showing another session's).
  local stream="$STATE_DIR/claude-stream-$tag.jsonl" current="$STATE_DIR/claude-current-$tag"
  # A wiki that reads with the model on this Mac never sends anything to Anthropic.
  if [ "$ENGINE" = local ]; then
    log "claude $label: refused, this wiki reads with the model on this Mac"
    return 1
  fi
  budget=$(config_value maxSpendPerBatchUsd "$(config_value maxSpendPerRunUsd 5)")
  command -v "$CLAUDE_BIN" >/dev/null 2>&1 || { log "claude not found on PATH"; return 127; }
  log "claude $label: start (max turns $turns, max spend \$$budget)"
  ev claude-start label="$label"
  nowp think detail="starting Claude"
  : > "$stream"; : > "$current"
  # Claude's actions stream to a file, and a follower turns them into events and log
  # lines. The watchdog below watches claude itself, never the follower.
  # While sessions overlap, a hook refuses Write on a page that already exists
  # (scripts/wiki_guard.py): two sessions creating the same page must not replace it.
  local guard=""
  [ "${LANES:-1}" -gt 1 ] && guard=$(guard_settings)
  "$CLAUDE_BIN" -p "$(cat "$prompt_file")" \
    ${guard:+--settings "$guard"} \
    --permission-mode dontAsk \
    --allowedTools "$ALLOWED_TOOLS" \
    --max-turns "$turns" \
    --max-budget-usd "$budget" \
    --output-format stream-json --verbose \
    > "$stream" 2>> "$LOG_FILE" < /dev/null &
  pid=$!
  "$PY" scripts/wiki_events.py claude-stream --file "$stream" --pid "$pid" --label "$label" \
    --current "$current" ${batch:+--batch "$batch"} >> "$LOG_FILE" 2>&1 &
  fpid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 2; waited=$((waited + 2))   # short: a finished batch frees its lane quickly
    if [ "$waited" -ge "$CLAUDE_TIMEOUT_SECONDS" ]; then
      log "claude $label: watchdog stopped it after ${waited}s"
      STALLED_ON=$(cat "$current" 2>/dev/null)
      kill "$pid" 2>/dev/null; sleep 5; kill -9 "$pid" 2>/dev/null
      stopped=1
      break
    fi
  done
  wait "$pid" 2>/dev/null; rc=$?
  wait "$fpid" 2>/dev/null
  outcome=$(claude_outcome "$stream")
  log "claude $label: exit $rc after ${waited}s (${outcome:-no result})"
  [ "$stopped" = 1 ] && return 4
  case "$outcome" in
    success) return 0 ;;
    error_max*) log "claude $label: stopped at its cap; the files it did not reach count as tried"; return 3 ;;
  esac
  [ "$rc" = 0 ] && return 0
  return 1
}

# ---------------------------------------------------------- local model ---------
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || date +%s; }

# One run of scripts/local_engine.py (one batch, or the packets). Returns 0 done (1 from
# the engine means some files failed: they stay queued and count a try, like any file);
# 2 the model could not run; 4 stopped by the watchdog. The model streams its answers and
# every step updates the live line, so "no progress" is the live line not changing for
# LOCAL_STALL_SECONDS: a slow Mac is never cut off, a hung model is.
run_local() { # local_engine.py run arguments
  local pid waited=0 rc idle stopped=0
  log "local model: start ($*)"
  "$PY" scripts/local_engine.py run "$@" >> "$LOG_FILE" 2>&1 < /dev/null &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 5; waited=$((waited + 5))
    idle=$(( $(date +%s) - $(mtime "$STATE_DIR/now.json") ))
    if [ "$idle" -ge "$LOCAL_STALL_SECONDS" ] || [ "$waited" -ge "$LOCAL_TIMEOUT_SECONDS" ]; then
      log "local model: watchdog stopped it after ${waited}s (no progress for ${idle}s)"
      STALLED_ON=$(now_file)
      kill "$pid" 2>/dev/null; sleep 40
      pkill -P "$pid" 2>/dev/null   # the model server, if it outlived its parent
      kill -9 "$pid" 2>/dev/null
      stopped=1
      break
    fi
  done
  wait "$pid" 2>/dev/null; rc=$?
  log "local model: exit $rc after ${waited}s"
  [ "$stopped" = 1 ] && return 4
  [ "$rc" -le 1 ] && return 0
  return 2
}

# ------------------------------------------------------------ attempts ---------
# One line per waiting item that has been tried: "<count><TAB><path>". A file tried this
# run and still waiting counts one more; one not tried this run (its batch never ran)
# keeps its count; one no longer waiting is forgotten. An item still waiting after
# MAX_ATTEMPTS tries is parked in raw/_needs-review/.
bump_attempts() { # file listing the paths tried this run
  local tmp="$STATE_DIR/attempts.new"
  { list_pending raw/_intake; list_pending raw/inbox; } > "$STATE_DIR/pending.now"
  "$PY" - "$ATTEMPTS_FILE" "$1" "$STATE_DIR/pending.now" "$tmp" <<'PYEOF' || return 0
import sys
old, tried, pending, out = sys.argv[1:5]
def lines(path):
    try:
        return [l.rstrip("\n") for l in open(path, encoding="utf-8") if l.strip()]
    except OSError:
        return []
counts = {}
for line in lines(old):
    n, _, path = line.partition("\t")
    if path and n.isdigit():
        counts[path] = int(n)
tried = set(lines(tried))
with open(out, "w", encoding="utf-8") as f:
    for path in sorted(set(lines(pending))):
        n = counts.get(path, 0) + (1 if path in tried else 0)
        if n:
            f.write(f"{n}\t{path}\n")
PYEOF
  mv "$tmp" "$ATTEMPTS_FILE"
}

park_repeat_failures() {
  local parked=0 n p b dest today
  [ -f "$ATTEMPTS_FILE" ] || return 0
  today=$(date +%F)
  while IFS="$(printf '\t')" read -r n p; do
    [ -n "$p" ] && [ -f "$p" ] || continue
    [ "$n" -ge "$MAX_ATTEMPTS" ] || continue
    b=$(basename "$p")
    mkdir -p raw/_needs-review
    dest="raw/_needs-review/$b"
    [ -e "$dest" ] && dest="raw/_needs-review/$(date +%Y%m%d%H%M%S)-$b"
    mv "$p" "$dest" || continue
    rid=$("$PY" scripts/wiki_review.py park --file "$p" --to "$dest" --attempts "$n" --engine "$ENGINE" 2>> "$LOG_FILE")
    {
      printf '\n## [%s] needs-review | general | %s | could not be processed after %s attempts\n' "$today" "$dest" "$n"
      printf -- '- The runner tried %s times and the file is still unprocessed, so it was moved out of the queue to stop retrying.\n' "$n"
      if [ -n "$rid" ]; then
        printf -- '- Review item: `%s`. Answer it on the Upload page, in the Review tab: read it again, set it aside, or say what it is.\n' "$rid"
      else
        printf -- '- Check that it opens and is readable, then upload it again (Upload, top right of the wiki).\n'
      fi
    } >> wiki/_review.md
    log "parked $p -> $dest after $n attempts${rid:+ (review item $rid)}"
    ev parked file="$p" to="$dest" attempts:="$n" review="$rid"
    parked=$((parked + 1))
  done < "$ATTEMPTS_FILE"
  if [ "$parked" -gt 0 ]; then
    notify "Wiki needs attention" "$parked file(s) could not be processed. Answer what to do with them in the Upload page's Review tab."
  fi
}

# -------------------------------------------------------------- ledger ---------
csv_field() { printf '"%s"' "$(printf '%s' "$1" | sed 's/"/""/g')"; }

append_ledger() { # before-snapshot after-snapshot run-id
  local new p now
  new=$(LC_ALL=C comm -13 "$1" "$2")
  [ -n "$new" ] || return 0
  now=$(date '+%Y-%m-%d %H:%M')
  mkdir -p archive
  [ -f "$LEDGER" ] || echo "ingested,path,run" > "$LEDGER"
  printf '%s\n' "$new" | while IFS= read -r p; do
    printf '%s,%s,%s\n' "$now" "$(csv_field "$p")" "$3" >> "$LEDGER"
  done
}

# What actually happened to each file this run started with, read from the folders: the
# authority for the Upload page when Claude's own action stream missed a step.
confirm_done() { # before-snapshot after-snapshot
  local new p b dest
  new=$(LC_ALL=C comm -13 "$1" "$2")
  printf '%s\n' "$DOCS_BEFORE" | sed '/^$/d' | while IFS= read -r p; do
    [ -e "$p" ] && continue
    b=$(basename "$p")
    dest=$(printf '%s\n' "$new" | awk -v b="/$b" 'length($0) >= length(b) && substr($0, length($0) - length(b) + 1) == b' | head -1)
    [ -n "$dest" ] && ev filed file="$p" to="$dest" confirmed:=true
  done
  printf '%s\n' "$PACKETS_BEFORE" | sed '/^$/d' | while IFS= read -r p; do
    [ -e "$p" ] && continue
    b=$(basename "$p")
    [ -f "archive/inbox/$b" ] && ev applied file="$p" to="archive/inbox/$b" confirmed:=true
  done
  return 0
}

# -------------------------------------------------------- tidy / commit --------
tidy() {
  nowp tidy detail="checking page headers and the ingestion register"
  "$PY" scripts/lint_frontmatter.py >> "$LOG_FILE" 2>&1 || log "lint_frontmatter failed"
  "$PY" scripts/build_ingestion_register.py >> "$LOG_FILE" 2>&1 || log "ingestion register failed"
  # Folders dropped into Intake leave empty shells behind once their files are filed.
  find raw/_intake -mindepth 1 -type d -empty -delete 2>/dev/null || true
  "$PY" scripts/wiki_review.py tidy >> "$LOG_FILE" 2>&1 || true
}

commit_changes() { # message
  [ -d .git ] || return 0
  git add -A -- wiki index.md log.md archive generated review >> "$LOG_FILE" 2>&1 || true
  if git diff --cached --quiet; then return 0; fi
  nowp commit detail="recording this version in the wiki's history"
  git -c user.name="Wiki runner" -c user.email="runner@localhost" \
    commit -q -m "$1" >> "$LOG_FILE" 2>&1 && log "committed: $1" && ev committed msg="$1"
}

build_site() { # [quiet]: a rebuild during a run, which leaves the live line alone and
  # fails silently (the next rebuild, or the one at the end, catches up)
  local next="$STATE_DIR/public.next" old="$STATE_DIR/public.old" quiet="${1:-}"
  if [ ! -f quartz/bootstrap-cli.mjs ] || [ ! -d node_modules ]; then
    log "site build skipped: Quartz is not installed here"
    return 1
  fi
  rm -rf "$next" "$old"
  if [ -z "$quiet" ]; then
    ev build-start
    nowp build detail="rebuilding the website"
  fi
  if "$NODE_BIN" quartz/bootstrap-cli.mjs build -d wiki -o "$next" >> "$LOG_FILE" 2>&1 \
     && [ -f "$next/index.html" ]; then
    # Swap in one step so the viewer never serves a half-built site.
    [ -d public ] && mv public "$old"
    mv "$next" public
    rm -rf "$old"
    log "site rebuilt"
    ev build-done
    return 0
  fi
  rm -rf "$next"
  log "site build FAILED (the previous site is still being served)"
  [ -z "$quiet" ] && ev build-failed
  return 1
}

# Whose wiki this is (Settings, "company" in wiki.config.json), so Claude can tell the
# owner's business from the other companies in a document.
business_note() {
  local c; c=$(config_value company "")
  [ -n "$c" ] && printf '\nThis wiki belongs to %s: when a document names it, that is the owner'"'"'s own business.\n' "$c"
  return 0
}

# The intake instructions, limited to one batch.
batch_prompt() { # batch-file
  cat engine/prompts/intake.md
  business_note
  printf '\nThis run is one batch of a larger upload. Process ONLY these documents, and leave every other file in raw/_intake/ exactly where it is (later batches handle them):\n\n'
  sed 's/^/- /' "$1"
  "$PY" scripts/wiki_review.py notes --batch "$1" 2>> "$LOG_FILE"
  if [ "${LANES:-1}" -gt 1 ]; then
    printf '\nOther sessions are filing other batches into this wiki at the same time. Pages you share with them (index.md, log.md, wiki/_review.md, and any company, person, topic or project page) can change while you work: read such a page right before you change it, change it with Edit (never rewrite it whole with Write), and if an edit is refused because the file changed, read it again and redo the edit. Write only creates new files: a Write to a file that already exists is refused, because another session may have just created it; read that file and add to it with Edit.\n'
  fi
}

# ---------------------------------------------------------------- main ---------
REBUILD_ONLY=0
SETTLE=1
for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD_ONLY=1 ;;
    --no-settle) SETTLE=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if ! acquire_lock; then
  log "another run is in progress; exiting"
  if [ -t 1 ]; then
    printf 'The wiki is already processing (since %s). New drops are picked up when it finishes.\n' \
      "$(cat "$LOCK_DIR/started" 2>/dev/null || echo 'a moment ago')"
    printf 'Watch it live:  tail -f %s\n' "$LOG_FILE"
  fi
  exit 0
fi
trap finish_run EXIT
refresh_starter_pages

if [ "$REBUILD_ONLY" = 1 ]; then
  remove_old_desktop_links
  build_site; exit $?
fi

RUN_ID="run-$(date +%Y%m%d-%H%M%S)"
export WIKI_RUN_ID="$RUN_ID"
mkdir -p raw/inbox raw/_intake archive/inbox review/open review/done
route_misfiled

if [ -z "$(list_pending raw/_intake)$(list_pending raw/inbox)" ]; then
  # Nothing to do. Make sure there is a site to look at, then stop quietly.
  [ -f public/index.html ] || build_site
  exit 0
fi

if [ "$SETTLE" = 1 ]; then
  ev waiting msg="Waiting for files to finish copying"
  nowp settle detail="checking that every upload and copy has finished"
  wait_for_settle
fi
route_misfiled
ENGINE=$(config_value engine claude)
[ "$ENGINE" = local ] || load_credentials
# How Claude reads: "fast" (the default), one Claude call per document, many at once,
# with the engine checking every answer and writing the pages (scripts/local_engine.py
# --reader claude); or "classic", a Claude Code session per batch that writes the pages
# itself.
PIPELINE=classic
if [ "$ENGINE" != local ] && [ "$(config_value pipeline fast)" != classic ]; then
  PIPELINE=fast
  export CLAUDE_BIN
fi

DOCS_BEFORE=$(list_pending raw/_intake)
PACKETS_BEFORE=$(list_pending raw/inbox)
N_DOCS=$(count_lines "$DOCS_BEFORE")
N_PACKETS=$(count_lines "$PACKETS_BEFORE")
BATCH_SIZE=$(config_value docsPerBatch "")
case "$BATCH_SIZE" in ''|*[!0-9]*|0) if [ "$ENGINE" = local ]; then BATCH_SIZE=20; else BATCH_SIZE=8; fi ;; esac
if [ "$PIPELINE" = fast ]; then   # many documents per batch: Claude reads them all at once
  BATCH_SIZE=$(config_value fast.docsPerBatch 40)
  case "$BATCH_SIZE" in ''|*[!0-9]*|0) BATCH_SIZE=40 ;; esac
fi
BATCH_DIR="$STATE_DIR/batches"; TRIED="$STATE_DIR/tried.txt"
rm -rf "$BATCH_DIR"; mkdir -p "$BATCH_DIR"; : > "$TRIED"
[ -n "$DOCS_BEFORE" ] && printf '%s\n' "$DOCS_BEFORE" | split -a 4 -l "$BATCH_SIZE" - "$BATCH_DIR/batch."
N_BATCHES=$(ls "$BATCH_DIR" | wc -l | tr -d ' ')
log "$RUN_ID: $N_DOCS document(s), $N_PACKETS packet(s) waiting"
# How Claude is paid for, so the Upload page can say what its cost estimate means.
AUTH=login
if [ "$ENGINE" = local ]; then AUTH=local
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then AUTH=api
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then AUTH=plan; fi
ev run-start docs:="$N_DOCS" packets:="$N_PACKETS" auth="$AUTH"
printf '%s\n%s\n' "$DOCS_BEFORE" "$PACKETS_BEFORE" | sed '/^$/d' | while IFS= read -r p; do
  ev queued file="$p"
done
BATCH_NOTE=""; [ "$N_BATCHES" -gt 1 ] && BATCH_NOTE=" in $N_BATCHES batches"
notify "Wiki is working" "Reading $N_DOCS document(s)$BATCH_NOTE and $N_PACKETS packet(s). This can take a while; another notice follows when it is done."

SNAP_BEFORE="$STATE_DIR/snap.before"; SNAP_AFTER="$STATE_DIR/snap.after"
snapshot_filed_sources > "$SNAP_BEFORE"
ARCHIVED_BEFORE=$(find archive/inbox -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')

# Documents, in batches. Conversion runs ahead in the background, so reading never
# waits for it. Claude reads up to LANES batches at once (parallelBatches, default 3);
# the first batch runs alone, so a bad sign-in fails after one call and the pages every
# batch shares (the business, the project) exist before several sessions edit them. The
# model on this Mac, and the fast pipeline (whose parallelism is inside each batch: Claude
# reads many of its documents at once), read one batch at a time.
# A batch that ran (even one stopped at Claude's cap) counts as a try for its files; a
# batch that never ran counts nothing. Claude or the model failing outright stops the
# run; so does a hang, which counts a try only for the file it hung on.
LANES=1
if [ "$ENGINE" != local ] && [ "$PIPELINE" != fast ]; then
  LANES=$(config_value parallelBatches 3)
  case "$LANES" in ''|*[!0-9]*|0) LANES=3 ;; esac
  [ "$LANES" -gt 6 ] && LANES=6
fi
LANE_DIR="$STATE_DIR/lanes"; rm -rf "$LANE_DIR"; mkdir -p "$LANE_DIR"
# Each session keeps its own stream; the last run's go now (and the single stream of
# engines before this one).
rm -f "$STATE_DIR"/claude-stream*.jsonl "$STATE_DIR"/claude-current-*
BATCHES=$(ls "$BATCH_DIR" | tr '\n' ' ')
BUILD_EVERY_SECONDS=180
CLAUDE_OK=1; STOP=""; STALLED_ON=""

convert_list() { # list-file start [--quiet]: one conversion process
  "$PY" scripts/to_markdown.py --only _intake --list "$1" 2>&1 \
    | "$PY" scripts/wiki_events.py convert-stream --total "$N_DOCS" --start "$2" ${3:-}
  [ "${PIPESTATUS[0]}" = 0 ] || log "to_markdown reported errors"
}

# The fast pipeline reads faster than one process can convert scans with OCR: each of its
# batches is converted by CONVERT_JOBS processes at once (half the Mac's cores, at most 8;
# fast.convertJobs in wiki.config.json overrides).
CONVERT_JOBS=1
if [ "$PIPELINE" = fast ]; then
  CONVERT_JOBS=$(config_value fast.convertJobs "")
  case "$CONVERT_JOBS" in
    ''|*[!0-9]*|0) CONVERT_JOBS=$(( $(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 2) / 2 )) ;;
  esac
  [ "$CONVERT_JOBS" -lt 1 ] && CONVERT_JOBS=1
  [ "$CONVERT_JOBS" -gt 8 ] && CONVERT_JOBS=8
fi

convert_batches() { # every batch in order, each marked ready once converted
  local b n started=0 quiet="" part pids at
  for b in $BATCHES; do
    [ -f "$LANE_DIR/stop" ] && break
    n=$(wc -l < "$BATCH_DIR/$b" | tr -d ' ')
    if [ "$CONVERT_JOBS" -gt 1 ] && [ "$n" -gt 1 ]; then
      rm -f "$LANE_DIR/$b.part."*
      awk -v j="$CONVERT_JOBS" -v out="$LANE_DIR/$b.part." '{ print > (out (NR - 1) % j) }' "$BATCH_DIR/$b"
      pids=""; at=$started
      for part in "$LANE_DIR/$b.part."*; do
        convert_list "$part" "$at" "$quiet" &
        pids="$pids $!"; at=$((at + $(wc -l < "$part" | tr -d ' ')))
        quiet=--quiet   # one process speaks for the live line
      done
      wait $pids
    else
      convert_list "$BATCH_DIR/$b" "$started" "$quiet"
    fi
    started=$((started + n))
    : > "$LANE_DIR/$b.ready"
    quiet=--quiet   # from here on the live line belongs to the reading
  done
}

read_batch() { # batch k: runs in the background; leaves its outcome in the lane folder
  local b="$1" k="$2" n rc
  n=$(wc -l < "$BATCH_DIR/$b" | tr -d ' ')
  [ "$N_BATCHES" -gt 1 ] && export WIKI_BATCH="$k/$N_BATCHES"
  log "batch $k of $N_BATCHES: $n document(s)"
  ev batch n:="$k" of:="$N_BATCHES" docs:="$n"
  STALLED_ON=""
  if [ "$ENGINE" = local ]; then
    run_local --docs "$BATCH_DIR/$b"; rc=$?
  elif [ "$PIPELINE" = fast ]; then
    if command -v "$CLAUDE_BIN" >/dev/null 2>&1; then run_local --docs "$BATCH_DIR/$b" --reader claude; rc=$?
    else log "claude not found on PATH"; rc=2; fi
  else
    batch_prompt "$BATCH_DIR/$b" > "$LANE_DIR/$b.prompt.md"
    run_claude "intake#$k" "$LANE_DIR/$b.prompt.md" 100 "$BATCH_DIR/$b"; rc=$?
  fi
  printf '%s' "$STALLED_ON" > "$LANE_DIR/$b.stalled"
  printf '%s' "$rc" > "$LANE_DIR/$b.rc"
}

finish_batch() { # batch: count its tries; a failure stops new batches
  local b="$1" rc stalled
  rc=$(cat "$LANE_DIR/$b.rc" 2>/dev/null); stalled=$(cat "$LANE_DIR/$b.stalled" 2>/dev/null)
  case "$rc" in
    0|3) cat "$BATCH_DIR/$b" >> "$TRIED" ;;
    4) if [ -n "$stalled" ]; then printf '%s\n' "$stalled" >> "$TRIED"; STALLED_ON="$stalled"
       else cat "$BATCH_DIR/$b" >> "$TRIED"; fi
       [ "$STOP" = engine ] || STOP=stalled; CLAUDE_OK=0 ;;
    *) STOP=engine; CLAUDE_OK=0 ;;
  esac
}

# Pages appear while a long upload is being read: the site is rebuilt in the background
# after a batch, at most every BUILD_EVERY_SECONDS.
BUILDER=""; LAST_BUILD=-100000
build_soon() {
  if [ -n "$BUILDER" ] && kill -0 "$BUILDER" 2>/dev/null; then return 0; fi
  [ $((SECONDS - LAST_BUILD)) -ge "$BUILD_EVERY_SECONDS" ] || return 0
  LAST_BUILD=$SECONDS
  build_site quiet &
  BUILDER=$!
}

convert_batches &
CONVERTER=$!
queue="$BATCHES"; running=""; k=0; first_done=0
while :; do
  still=""
  for entry in $running; do
    if kill -0 "${entry%%:*}" 2>/dev/null; then still="$still $entry"; continue; fi
    wait "${entry%%:*}" 2>/dev/null
    finish_batch "${entry#*:}"
    first_done=1
    [ "$CLAUDE_OK" = 1 ] && build_soon
  done
  running="$still"
  busy=$(printf '%s\n' $running | sed '/^$/d' | wc -l | tr -d ' ')
  limit=$LANES; [ "$first_done" = 1 ] || limit=1
  while [ "$CLAUDE_OK" = 1 ] && [ -n "$(printf '%s' "$queue" | tr -d ' ')" ] && [ "$busy" -lt "$limit" ]; do
    b=$(printf '%s\n' $queue | head -1)
    if [ ! -f "$LANE_DIR/$b.ready" ]; then
      kill -0 "$CONVERTER" 2>/dev/null && break
      : > "$LANE_DIR/$b.ready"   # the converter is gone: Claude notes any missing text
    fi
    queue=$(printf '%s\n' $queue | sed 1d | tr '\n' ' ')
    k=$((k + 1))
    read_batch "$b" "$k" &
    running="$running $!:$b"; busy=$((busy + 1))
  done
  if [ -z "$(printf '%s' "$running" | tr -d ' ')" ]; then
    [ "$CLAUDE_OK" = 1 ] && [ -n "$(printf '%s' "$queue" | tr -d ' ')" ] || break
  fi
  sleep 1
done
: > "$LANE_DIR/stop"
wait "$CONVERTER" 2>/dev/null; CONVERTER=""
[ -n "$BUILDER" ] && wait "$BUILDER" 2>/dev/null; BUILDER=""
unset WIKI_BATCH

# Update Packets, after the documents.
if [ "$ENGINE" = local ]; then
  MAX_INGEST_ROUNDS=0   # the model on this Mac applies them in one call
  if [ "$CLAUDE_OK" = 1 ] && [ -n "$(list_packets)" ]; then
    LOCAL_PACKETS=$(list_packets)
    run_local --packets; rc=$?
    case "$rc" in
      0) printf '%s\n' "$LOCAL_PACKETS" >> "$TRIED" ;;
      4) [ -n "$STALLED_ON" ] && printf '%s\n' "$STALLED_ON" >> "$TRIED"; STOP=stalled; CLAUDE_OK=0 ;;
      *) STOP=engine; CLAUDE_OK=0 ;;
    esac
  fi
fi

round=0
while [ "$CLAUDE_OK" = 1 ] && [ "$round" -lt "$MAX_INGEST_ROUNDS" ]; do
  ROUND_PACKETS=$(list_packets)
  [ -n "$ROUND_PACKETS" ] || break
  round=$((round + 1))
  { cat engine/prompts/ingest.md; business_note; } > "$LANE_DIR/ingest.prompt.md"
  run_claude "ingest#$round" "$LANE_DIR/ingest.prompt.md" 60; rc=$?
  case "$rc" in
    0|3) printf '%s\n' "$ROUND_PACKETS" >> "$TRIED" ;;
    4) printf '%s\n' "$ROUND_PACKETS" >> "$TRIED"; STOP=stalled; CLAUDE_OK=0; break ;;
    *) STOP=engine; CLAUDE_OK=0; break ;;
  esac
  # Progress means packets that were waiting at the start of this round are gone;
  # files dropped meanwhile do not count against it.
  remaining=0
  while IFS= read -r p; do
    [ -f "$p" ] && remaining=$((remaining + 1))
  done <<ROUND
$ROUND_PACKETS
ROUND
  if [ "$remaining" -ge "$(count_lines "$ROUND_PACKETS")" ]; then
    log "ingest#$round made no progress ($remaining packet(s) left); stopping this run"
    break
  fi
done

# Tries: only files in what actually ran.
bump_attempts "$TRIED"
park_repeat_failures
STILL_TRIED=$( { list_pending raw/_intake; list_pending raw/inbox; } | LC_ALL=C sort \
  | LC_ALL=C comm -12 - <(sed '/^$/d' "$TRIED" | LC_ALL=C sort -u) )
printf '%s\n' "$STILL_TRIED" | sed '/^$/d' | while IFS= read -r p; do
  [ -f "$p" ] && ev retry file="$p"
done
LEFT=$(( $(count_lines "$(list_pending raw/_intake)") ))
case "$STOP:$ENGINE" in
  engine:local) notify_error "The local model could not run. See ~/Library/Logs/wiki-starter/runner.log, or run the installer again to repair it." ;;
  engine:*) notify_error "Claude could not run. Check your Anthropic sign-in (run the installer again), then press Process now on the Upload page." ;;
  stalled:local) notify_error "The model on this Mac stopped responding${STALLED_ON:+ while reading $(basename "$STALLED_ON")}. The $LEFT document(s) still waiting will be read on the next run." ;;
  stalled:*) notify_error "Claude stopped responding during a batch. The $LEFT document(s) still waiting will be read on the next run." ;;
esac

snapshot_filed_sources > "$SNAP_AFTER"
append_ledger "$SNAP_BEFORE" "$SNAP_AFTER" "$RUN_ID"
tidy

# Count what was actually filed and archived, not what left the queue: a parked
# file leaves too, and must not be reported as done.
DONE_DOCS=$(LC_ALL=C comm -13 "$SNAP_BEFORE" "$SNAP_AFTER" | sed '/^$/d' | wc -l | tr -d ' ')
DONE_PACKETS=$(( $(find archive/inbox -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ') - ARCHIVED_BEFORE ))
[ "$DONE_PACKETS" -lt 0 ] && DONE_PACKETS=0
confirm_done "$SNAP_BEFORE" "$SNAP_AFTER"
commit_changes "wiki: $(date +%F) $DONE_DOCS document(s), $DONE_PACKETS packet(s) ($RUN_ID)"

if build_site; then
  if [ $((DONE_DOCS + DONE_PACKETS)) -gt 0 ]; then
    notify "Wiki updated" "$DONE_DOCS document(s), $DONE_PACKETS packet(s) added. Open wiki pages show them by themselves."
  fi
else
  notify_error "The wiki was updated but the site could not be rebuilt. See ~/Library/Logs/wiki-starter/runner.log."
fi
log "$RUN_ID: done ($DONE_DOCS document(s), $DONE_PACKETS packet(s))"
ev run-end docs:="$DONE_DOCS" packets:="$DONE_PACKETS"
nowp idle

# Files dropped while this run was busy would otherwise wait for the 15-minute timer:
# the folder watcher fired, found this run holding the lock, and gave up. Start another
# run now (bounded, and never when Claude itself could not run).
ARRIVED=$( { list_pending raw/_intake; list_pending raw/inbox; } | LC_ALL=C sort \
  | LC_ALL=C comm -23 - <(printf '%s\n%s\n' "$DOCS_BEFORE" "$PACKETS_BEFORE" | sed '/^$/d' | LC_ALL=C sort) )
if [ "$CLAUDE_OK" = 1 ] && [ -n "$ARRIVED" ] && [ "${WIKI_RERUN_DEPTH:-0}" -lt 3 ]; then
  log "$(count_lines "$ARRIVED") file(s) arrived during this run; starting another run"
  ev rerun count:="$(count_lines "$ARRIVED")"
  release_lock
  trap - EXIT
  export WIKI_RERUN_DEPTH=$(( ${WIKI_RERUN_DEPTH:-0} + 1 ))
  exec /bin/bash "$WIKI_DIR/scripts/wiki_runner.sh"
fi
exit 0
