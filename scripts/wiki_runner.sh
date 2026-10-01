#!/bin/bash
# wiki_runner.sh — fold everything waiting in the drop folders into the wiki.
#
# Started by a LaunchAgent whenever something lands in raw/inbox/ (Wiki Inbox) or
# raw/_intake/ (Wiki Intake), every 15 minutes as a fallback, and at login. Also run by
# hand from "Process Now.command". A run:
#
#   1. takes a single-writer lock (a second run exits at once);
#   2. waits until dropped files have finished copying;
#   3. routes misfiled drops (a PDF in the Inbox goes to Intake; a packet in Intake
#      goes to the Inbox);
#   4. converts new documents to Markdown (scripts/to_markdown.py) and has Claude file
#      them (engine/prompts/intake.md);
#   5. has Claude apply Update Packets, five at a time (engine/prompts/ingest.md);
#   6. parks anything that failed twice in raw/_needs-review/ and says so in
#      wiki/_review.md, so a broken file never burns money every 15 minutes;
#   7. records newly filed documents in archive/ingestion-ledger.csv, tidies
#      frontmatter, refreshes the ingestion register;
#   8. commits the change to the folder's local git history (never pushed anywhere);
#   9. rebuilds the local site into public/ and shows a notification.
#
# Claude runs with --permission-mode dontAsk and an explicit tool allowlist: it can read,
# write and edit files in this folder and use mv/mkdir/ls, nothing else. Every call has a
# turn cap, a spending cap and a wall-clock watchdog.
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
ERROR_NOTIFY_EVERY_SECONDS="${WIKI_ERROR_NOTIFY_EVERY_SECONDS:-21600}"

ALLOWED_TOOLS="Read,Glob,Grep,Edit,Write,Bash(mv *),Bash(mkdir *),Bash(ls *)"

mkdir -p "$STATE_DIR" "$LOG_DIR"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"; [ -t 1 ] && printf '%s\n' "$*"; return 0; }

config_value() { # key default
  "$PY" - "$1" "$2" <<'PYEOF' 2>/dev/null || printf '%s' "$2"
import json, sys
key, default = sys.argv[1], sys.argv[2]
try:
    v = json.load(open("wiki.config.json")).get(key)
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
}
release_lock() { rm -rf "$LOCK_DIR"; }

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

partial_downloads_present() {
  find raw/inbox raw/_intake -type f \( -name '*.download' -o -name '*.crdownload' -o -name '*.part' -o -name '*.partial' \) 2>/dev/null | grep -q .
}

drop_signature() { ls -lnR raw/inbox raw/_intake 2>/dev/null | cksum; }

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

route_misfiled() {
  local f b
  # A document dropped into the Inbox belongs in Intake.
  list_pending raw/inbox | while IFS= read -r f; do
    case "$f" in *.md|*.markdown) continue ;; esac
    b=$(basename "$f")
    mkdir -p raw/_intake
    if [ -e "raw/_intake/$b" ]; then b="$(date +%Y%m%d%H%M%S)-$b"; fi
    mv "$f" "raw/_intake/$b" && log "routed $f -> raw/_intake/$b"
  done
  # A packet dropped into Intake belongs in the Inbox.
  list_pending raw/_intake | while IFS= read -r f; do
    case "$f" in *.md) ;; *) continue ;; esac
    looks_like_packet "$f" || continue
    b=$(basename "$f")
    if [ -e "raw/inbox/$b" ]; then b="$(date +%Y%m%d%H%M%S)-$b"; fi
    mv "$f" "raw/inbox/$b" && log "routed $f -> raw/inbox/$b"
  done
}

snapshot_filed_sources() {
  find raw -type f \
    ! -path 'raw/inbox/*' ! -path 'raw/_intake/*' ! -path 'raw/_needs-review/*' \
    ! -name '.*' 2>/dev/null | LC_ALL=C sort
}

# ------------------------------------------------------------- claude ----------
run_claude() { # label prompt-file max-turns
  local label="$1" prompt_file="$2" turns="$3" budget pid waited=0 rc
  budget=$(config_value maxSpendPerRunUsd 5)
  command -v "$CLAUDE_BIN" >/dev/null 2>&1 || { log "claude not found on PATH"; return 127; }
  log "claude $label: start (max turns $turns, max spend \$$budget)"
  "$CLAUDE_BIN" -p "$(cat "$prompt_file")" \
    --permission-mode dontAsk \
    --allowedTools "$ALLOWED_TOOLS" \
    --max-turns "$turns" \
    --max-budget-usd "$budget" \
    >> "$LOG_FILE" 2>&1 < /dev/null &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 5; waited=$((waited + 5))
    if [ "$waited" -ge "$CLAUDE_TIMEOUT_SECONDS" ]; then
      log "claude $label: watchdog stopped it after ${waited}s"
      kill "$pid" 2>/dev/null; sleep 5; kill -9 "$pid" 2>/dev/null
      break
    fi
  done
  wait "$pid" 2>/dev/null; rc=$?
  log "claude $label: exit $rc after ${waited}s"
  return "$rc"
}

# ------------------------------------------------------------ attempts ---------
# One line per pending item: "<count><TAB><path>". An item still pending after
# MAX_ATTEMPTS successful Claude runs is parked in raw/_needs-review/.
bump_attempts() { # newline-separated paths still pending after a successful run
  local still="$1" tmp="$STATE_DIR/attempts.new" p n
  : > "$tmp"
  [ -n "$still" ] && printf '%s\n' "$still" | while IFS= read -r p; do
    n=$(awk -F '\t' -v p="$p" '$2 == p { print $1 }' "$ATTEMPTS_FILE" 2>/dev/null | head -1)
    printf '%s\t%s\n' "$(( ${n:-0} + 1 ))" "$p" >> "$tmp"
  done
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
    {
      printf '\n## [%s] needs-review | general | %s | could not be processed after %s attempts\n' "$today" "$dest" "$n"
      printf -- '- The runner tried %s times and the file is still unprocessed, so it was moved out of the drop folder to stop retrying.\n' "$n"
      printf -- '- Check that it opens and is readable, then drop it into Wiki Intake (documents) or Wiki Inbox (packets) again.\n'
    } >> wiki/_review.md
    log "parked $p -> $dest after $n attempts"
    parked=$((parked + 1))
  done < "$ATTEMPTS_FILE"
  if [ "$parked" -gt 0 ]; then
    notify "Wiki needs attention" "$parked file(s) could not be processed and were moved to raw/_needs-review. See the Review queue."
    : > "$ATTEMPTS_FILE"
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

# -------------------------------------------------------- tidy / commit --------
tidy() {
  "$PY" scripts/lint_frontmatter.py >> "$LOG_FILE" 2>&1 || log "lint_frontmatter failed"
  "$PY" scripts/build_ingestion_register.py >> "$LOG_FILE" 2>&1 || log "ingestion register failed"
  # Folders dropped into Intake leave empty shells behind once their files are filed.
  find raw/_intake -mindepth 1 -type d -empty -delete 2>/dev/null || true
}

commit_changes() { # message
  [ -d .git ] || return 0
  git add -A -- wiki index.md log.md archive generated >> "$LOG_FILE" 2>&1 || true
  if git diff --cached --quiet; then return 0; fi
  git -c user.name="Wiki runner" -c user.email="runner@localhost" \
    commit -q -m "$1" >> "$LOG_FILE" 2>&1 && log "committed: $1"
}

build_site() {
  local next="$STATE_DIR/public.next" old="$STATE_DIR/public.old"
  if [ ! -f quartz/bootstrap-cli.mjs ] || [ ! -d node_modules ]; then
    log "site build skipped: Quartz is not installed here"
    return 1
  fi
  rm -rf "$next" "$old"
  if "$NODE_BIN" quartz/bootstrap-cli.mjs build -d wiki -o "$next" >> "$LOG_FILE" 2>&1 \
     && [ -f "$next/index.html" ]; then
    # Swap in one step so the viewer never serves a half-built site.
    [ -d public ] && mv public "$old"
    mv "$next" public
    rm -rf "$old"
    log "site rebuilt"
    return 0
  fi
  rm -rf "$next"
  log "site build FAILED (the previous site is still being served)"
  return 1
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
  exit 0
fi
trap release_lock EXIT

if [ "$REBUILD_ONLY" = 1 ]; then
  build_site; exit $?
fi

mkdir -p raw/inbox raw/_intake archive/inbox
route_misfiled

if [ -z "$(list_pending raw/_intake)$(list_pending raw/inbox)" ]; then
  # Nothing to do. Make sure there is a site to look at, then stop quietly.
  [ -f public/index.html ] || build_site
  exit 0
fi

[ "$SETTLE" = 1 ] && wait_for_settle
route_misfiled
load_credentials

RUN_ID="run-$(date +%Y%m%d-%H%M%S)"
DOCS_BEFORE=$(list_pending raw/_intake)
PACKETS_BEFORE=$(list_pending raw/inbox)
N_DOCS=$(count_lines "$DOCS_BEFORE")
N_PACKETS=$(count_lines "$PACKETS_BEFORE")
log "$RUN_ID: $N_DOCS document(s), $N_PACKETS packet(s) waiting"

SNAP_BEFORE="$STATE_DIR/snap.before"; SNAP_AFTER="$STATE_DIR/snap.after"
snapshot_filed_sources > "$SNAP_BEFORE"
ARCHIVED_BEFORE=$(find archive/inbox -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')

CLAUDE_OK=1
if [ "$N_DOCS" -gt 0 ]; then
  log "converting documents"
  "$PY" scripts/to_markdown.py --only _intake >> "$LOG_FILE" 2>&1 || log "to_markdown reported errors"
  run_claude intake engine/prompts/intake.md 100 || CLAUDE_OK=0
fi

round=0
while [ "$CLAUDE_OK" = 1 ] && [ "$round" -lt "$MAX_INGEST_ROUNDS" ]; do
  left=$(count_lines "$(list_pending raw/inbox)")
  [ "$left" -gt 0 ] || break
  round=$((round + 1))
  run_claude "ingest#$round" engine/prompts/ingest.md 60 || { CLAUDE_OK=0; break; }
  now_left=$(count_lines "$(list_pending raw/inbox)")
  if [ "$now_left" -ge "$left" ]; then
    log "ingest#$round made no progress ($now_left packet(s) left); stopping this run"
    break
  fi
done

STILL="$(list_pending raw/_intake)"
P_STILL="$(list_pending raw/inbox)"
[ -n "$P_STILL" ] && STILL="$(printf '%s\n%s' "$STILL" "$P_STILL" | sed '/^$/d')"
if [ "$CLAUDE_OK" = 1 ]; then
  # Count an attempt only against files that were waiting when this run started.
  ATTEMPTED=$(printf '%s\n%s\n' "$DOCS_BEFORE" "$PACKETS_BEFORE" | sed '/^$/d' | LC_ALL=C sort)
  STILL_ATTEMPTED=$(printf '%s\n' "$STILL" | sed '/^$/d' | LC_ALL=C sort | LC_ALL=C comm -12 - <(printf '%s\n' "$ATTEMPTED"))
  bump_attempts "$STILL_ATTEMPTED"
  park_repeat_failures
else
  notify_error "Claude could not run. Check your Anthropic sign-in (run the installer again), then use Process Now."
fi

snapshot_filed_sources > "$SNAP_AFTER"
append_ledger "$SNAP_BEFORE" "$SNAP_AFTER" "$RUN_ID"
tidy

# Count what was actually filed and archived, not what left the drop folders: a parked
# file leaves too, and must not be reported as done.
DONE_DOCS=$(LC_ALL=C comm -13 "$SNAP_BEFORE" "$SNAP_AFTER" | sed '/^$/d' | wc -l | tr -d ' ')
DONE_PACKETS=$(( $(find archive/inbox -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ') - ARCHIVED_BEFORE ))
[ "$DONE_PACKETS" -lt 0 ] && DONE_PACKETS=0
commit_changes "wiki: $(date +%F) $DONE_DOCS document(s), $DONE_PACKETS packet(s) ($RUN_ID)"

if build_site; then
  if [ $((DONE_DOCS + DONE_PACKETS)) -gt 0 ]; then
    notify "Wiki updated" "$DONE_DOCS document(s), $DONE_PACKETS packet(s). Refresh the wiki to see them."
  fi
else
  notify_error "The wiki was updated but the site could not be rebuilt. See ~/Library/Logs/wiki-starter/runner.log."
fi
log "$RUN_ID: done ($DONE_DOCS document(s), $DONE_PACKETS packet(s))"
exit 0
