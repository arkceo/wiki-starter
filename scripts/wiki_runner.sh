#!/bin/bash
# wiki_runner.sh — fold everything waiting in the queue into the wiki.
#
# The Upload page puts documents in raw/_intake/ and Update Packets in raw/inbox/. A
# LaunchAgent starts this script whenever something lands there, every 15 minutes as a
# fallback, and at login. It also runs from the Upload page's "Process now" button and
# from "Process Now.command". A run:
#
#   1. takes a single-writer lock (a second run exits at once; a lock left by a run that
#      died is taken over, even when its process number now belongs to another program),
#      moves older settings to the performance modes once (scripts/wiki_settings.py), and
#      keeps the Mac from sleeping while it works (caffeinate);
#   2. waits until dropped files have finished copying: every file whose size and time
#      did not change over WIKI_SETTLE_SECONDS (5 s) is ready, and the run waits at most
#      WIKI_SETTLE_MAX_SECONDS (60 s) for the rest. It reads only the documents and
#      applies only the packets that were ready; those still changing (and uploads still
#      arriving: the Upload page puts each in the queue in one step) are read by another
#      run that starts as this one ends. Folders whose name starts with a dot are never
#      queued (the converter and the reader skip them too);
#   3. routes misfiled drops (a PDF in the Inbox goes to Intake; a packet in Intake
#      goes to the Inbox);
#   4. reads new documents in batches (the documents per batch of the performance mode,
#      or the owner's own figure; scripts/wiki_settings.py): each batch is converted to
#      Markdown (scripts/to_markdown.py, watched: a converter that crashes or hangs on a
#      file leaves that file marked unreadable and goes on with the rest; one that cannot
#      work at all stops the run with a notice, counting nothing) and then filed
#      by Claude (engine/prompts/intake.md) before the next batch starts, so a large drop
#      never outgrows one call's caps and pages appear while it is still going;
#   5. has Claude apply Update Packets, five at a time, naming them in the prompt
#      (engine/prompts/ingest.md);
#   6. parks anything that failed twice in raw/_needs-review/ and says so in
#      wiki/_review.md, so a broken file never burns money every 15 minutes (only a
#      batch that actually ran counts as a try for its files, and only for the documents
#      it got to), with a review item the owner (or Jev) answers on the Upload page
#      (scripts/wiki_review.py);
#   7. records newly filed documents in archive/ingestion-ledger.csv, tidies
#      frontmatter, refreshes the ingestion register;
#   8. commits the change to the folder's local git history (never pushed anywhere);
#   9. rebuilds the local site into public/ and shows a notification;
#  10. starts again at once (a few times at most) if files arrived meanwhile, or a
#      spending cap left documents unread.
#
# Every step is also written to .wiki-engine/state/events.jsonl (scripts/wiki_events.py),
# which the Upload page shows as per-file progress and a live log, and what is happening
# at this moment to .wiki-engine/state/now.json, its live line.
#
# Claude runs with --permission-mode dontAsk and an explicit tool allowlist: it can read,
# write and edit files in this folder and use mv/mkdir/ls, nothing else. Every call (one
# batch) has a turn cap, a spending cap (maxSpendPerBatchUsd, from the mode) and a
# wall-clock watchdog.
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
# Packets still go to a Claude session (step 5). Documents a batch never put to Claude
# because it reached its spending cap count no try (local_engine.py names them); every
# other document of a batch that ran counts one, also one it never saw. Claude out of
# reach (an offline Mac, Anthropic's service failing) stops the run like a sign-in that
# does not work: no document counts a try. If local_engine.py itself dies (a native
# crash), the documents it was reading at that moment count a try, as for a hang, and the
# next run reads them first, one at a time.
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
PROGRESS="$STATE_DIR/progress"   # touched when the work moves (scripts/wiki_events.py)
ATTEMPTS_FILE="$STATE_DIR/attempts.txt"
LEDGER="archive/ingestion-ledger.csv"
KEYCHAIN_SERVICE="wiki-starter"

CLAUDE_BIN="${CLAUDE_BIN:-claude}"
PY="${WIKI_PYTHON:-$WIKI_DIR/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
NODE_BIN="${NODE_BIN:-node}"

SETTLE_SECONDS="${WIKI_SETTLE_SECONDS:-5}"            # a file unchanged this long is ready
SETTLE_MAX_SECONDS="${WIKI_SETTLE_MAX_SECONDS:-60}"    # the longest wait for the others
MAX_INGEST_ROUNDS="${WIKI_MAX_INGEST_ROUNDS:-10}"
MAX_ATTEMPTS="${WIKI_MAX_ATTEMPTS:-2}"
MAX_RERUNS="${WIKI_MAX_RERUNS:-10}"                    # runs started one after another (step 10)
CLAUDE_TIMEOUT_SECONDS="${WIKI_CLAUDE_TIMEOUT_SECONDS:-2400}"
LOCAL_TIMEOUT_SECONDS="${WIKI_LOCAL_TIMEOUT_SECONDS:-21600}"   # one batch, in all
LOCAL_STALL_SECONDS="${WIKI_LOCAL_STALL_SECONDS:-1200}"        # no sign of progress
CONVERT_STALL_SECONDS="${WIKI_CONVERT_STALL_SECONDS:-1800}"    # a conversion with no sign of progress
ERROR_NOTIFY_EVERY_SECONDS="${WIKI_ERROR_NOTIFY_EVERY_SECONDS:-21600}"
BUILD_TIMEOUT_SECONDS="${WIKI_BUILD_TIMEOUT_SECONDS:-1800}"    # one site rebuild
KEYCHAIN_SECONDS="${WIKI_KEYCHAIN_SECONDS:-20}"                # one Keychain lookup

ALLOWED_TOOLS="Read,Glob,Grep,Edit,Write,Bash(mv *),Bash(mkdir *),Bash(ls *)"
TAB=$(printf '\t')

mkdir -p "$STATE_DIR" "$LOG_DIR"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"; [ -t 1 ] && printf '%s\n' "$*"; return 0; }

# One event for the Upload page: ev TYPE key=value ... (key:=json for numbers and lists).
ev() { "$PY" scripts/wiki_events.py emit "$@" >> "$LOG_FILE" 2>&1 || true; }

# The same event for every line on stdin, in one go: evs TYPE --key K [--key K] key=value ...
# A line's tab-separated values fill the --key fields. Thousands of files take a fraction of
# a second this way; one ev each would take minutes.
evs() { "$PY" scripts/wiki_events.py emit-each "$@" >> "$LOG_FILE" 2>&1 || true; }

# What is happening right now, for the Upload page's live line: nowp PHASE key=value ...
nowp() { "$PY" scripts/wiki_events.py now "$@" >> "$LOG_FILE" 2>&1 || true; }

# A reading setting as this wiki's performance mode (or the owner's own figure) sets it:
# docsPerBatch (for this wiki's way of reading), maxSpendPerBatchUsd, parallelBatches.
setting() { "$PY" scripts/wiki_settings.py get "$1" 2>> "$LOG_FILE"; }

thousands() { # 2835 -> 2,835
  local n="$1" out=""
  while [ "${#n}" -gt 3 ]; do out=",${n: -3}$out"; n="${n:0:${#n}-3}"; done
  printf '%s%s' "$n" "$out"
}

# stat differs: GNU (Linux, or coreutils first on PATH) says `stat -c`, the Mac's `stat -f`.
if stat -c %Y . >/dev/null 2>&1; then GNU_STAT=1; else GNU_STAT=0; fi
mtime() {
  if [ "$GNU_STAT" = 1 ]; then stat -c %Y "$1" 2>/dev/null || date +%s
  else stat -f %m "$1" 2>/dev/null || date +%s; fi
}

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
# The lock is held only while the process it names is alive AND is a run of this script
# AND started when the lock says: a run that died without cleaning up (a crash, SIGKILL,
# a power cut) leaves its lock behind, and the system may since have given its process
# number to another program, which must not keep every later run out (and the Upload page
# saying "Working" for ever). The start time is read with LC_ALL=C, in UTC (ps prints it in
# local time, and a laptop's time zone can change during a long run) and squeezed, so every
# reader compares the same text; a rerun (exec) keeps both the number and the time.
proc_started() { LC_ALL=C TZ=UTC0 ps -p "$1" -o lstart= 2>/dev/null | awk '{$1 = $1; print}'; }
lock_holder_alive() { # pid
  local cmd stamp
  kill -0 "$1" 2>/dev/null || return 1
  cmd=$(ps -p "$1" -o command= 2>/dev/null)
  [ -n "$cmd" ] || return 0   # it is alive and ps cannot say what it is: assume a run
  case "$cmd" in *wiki_runner.sh*) ;; *) return 1 ;; esac
  stamp=$(cat "$LOCK_DIR/lstart" 2>/dev/null)
  [ -z "$stamp" ] || [ "$stamp" = "$(proc_started "$1")" ]   # no stamp: an older engine's lock
}
take_lock() {
  echo $$ > "$LOCK_DIR/pid"
  proc_started $$ > "$LOCK_DIR/lstart"
  date '+%H:%M' > "$LOCK_DIR/started"
}
acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then take_lock; return 0; fi
  local pid holder=""
  pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -n "$pid" ] && lock_holder_alive "$pid"; then
    return 1
  fi
  # A run that took the lock this very moment may not have written its number yet.
  [ -z "$pid" ] && [ $(( $(date +%s) - $(mtime "$LOCK_DIR") )) -lt 10 ] && return 1
  [ -n "$pid" ] && holder=$(ps -p "$pid" -o command= 2>/dev/null)
  log "removing stale lock (pid ${pid:-unknown}, now ${holder:-no process})"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" 2>/dev/null || return 1
  take_lock
}
release_lock() { rm -rf "$LOCK_DIR"; }
# A run that ends early takes its background work (reading lanes, conversion, a site
# rebuild) with it, so nothing outlives the lock.
kill_tree() { # pid: it and everything it started (conversion runs several processes deep)
  local c
  for c in $(pgrep -P "$1" 2>/dev/null); do kill_tree "$c"; done
  kill "$1" 2>/dev/null
}
# Waits for a background process (a child of this script) at most SECONDS; past that it
# is stopped with everything it started. Returns its exit status, 124 when stopped. (The
# Mac has no timeout command.)
wait_at_most() { # pid seconds
  local pid="$1" left="$2"
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$left" -le 0 ]; then
      kill_tree "$pid"; wait "$pid" 2>/dev/null
      return 124
    fi
    sleep 1; left=$((left - 1))
  done
  wait "$pid" 2>/dev/null
}
stop_background() {
  local e p
  for e in ${running:-}; do
    p=${e%%:*}; kill_tree "$p"
  done
  for p in ${CONVERTER:-} ${BUILDER:-} ${AWAKE:-}; do kill_tree "$p"; done
}
# However a run ends, the live line must not keep describing a step that stopped.
finish_run() { stop_background; nowp idle; release_lock; }

# --------------------------------------------------------- credentials ---------
# One stored secret from the Keychain, or nothing. A Keychain that asks for permission
# with nobody at the Mac would wait for ever: after KEYCHAIN_SECONDS it counts as nothing
# stored (and the log says so).
keychain() { # account
  local spid tpid
  security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$1" -w 2>/dev/null & spid=$!
  ( sleep "$KEYCHAIN_SECONDS"; kill "$spid" 2>/dev/null && : > "$STATE_DIR/keychain-timeout" ) >/dev/null 2>&1 &
  tpid=$!
  wait "$spid" 2>/dev/null
  pkill -P "$tpid" 2>/dev/null; kill "$tpid" 2>/dev/null
  return 0
}
load_credentials() {
  [ -n "${ANTHROPIC_API_KEY:-}" ] && return 0
  [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && return 0
  command -v security >/dev/null 2>&1 || return 0
  local v account
  for account in claude-oauth-token anthropic-api-key; do
    rm -f "$STATE_DIR/keychain-timeout"
    v=$(keychain "$account")
    if [ -f "$STATE_DIR/keychain-timeout" ]; then
      rm -f "$STATE_DIR/keychain-timeout"
      log "the Keychain gave no answer within ${KEYCHAIN_SECONDS}s for $account; going on without it"
      continue
    fi
    [ -n "$v" ] || continue
    if [ "$account" = claude-oauth-token ]; then export CLAUDE_CODE_OAUTH_TOKEN="$v"; else export ANTHROPIC_API_KEY="$v"; fi
    return 0
  done
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

# Every file in a queue folder, sorted. Per-file work here is shell built-ins only: a
# command per file (basename) took seconds for a few thousand files, every listing. Folders
# whose name starts with a dot (.git, .Trashes, ...) are skipped, as the converter
# (scripts/to_markdown.py) and the reader (scripts/local_engine.py) skip them: a file only
# this listing saw would wait, untried, for ever.
list_pending() { # dir -> one path per line, sorted
  local d="$1" f
  [ -d "$d" ] || return 0
  find "$d" -mindepth 1 \( -type d -name '.*' -prune \) -o -type f -print 2>/dev/null | LC_ALL=C sort | while IFS= read -r f; do
    is_ignored_name "${f##*/}" && continue
    printf '%s\n' "$f"
  done
}

count_lines() { if [ -z "$1" ]; then echo 0; else printf '%s\n' "$1" | wc -l | tr -d ' '; fi; }

# Settling. Uploads from the Upload page reach the queue in one step (each is written
# elsewhere first), so a file in the queue is complete unless something is still copying
# into it, in Finder or a browser. One listing of every queued file's path, size and time
# per check: a file unchanged since the last check is ready; a new or changed one, or a
# download still named *.part, *.download, *.crdownload or *.partial (and written in the
# last five minutes: an older one was abandoned), is still arriving.
queue_stats() { # -> path<TAB>size<TAB>mtime per file, sorted (dot folders skipped, as listed)
  if [ "$GNU_STAT" = 1 ]; then
    find raw/inbox raw/_intake -mindepth 1 \( -type d -name '.*' -prune \) -o -type f -exec stat -c "%n$TAB%s$TAB%Y" {} + 2>/dev/null
  else
    find raw/inbox raw/_intake -mindepth 1 \( -type d -name '.*' -prune \) -o -type f -exec stat -f "%N$TAB%z$TAB%m" {} + 2>/dev/null
  fi | LC_ALL=C sort
}

# Compares two queue_stats listings; writes the paths ready in both to ready-file and
# prints "<files in the queue> <still arriving>". The name rules are is_ignored_name's. The
# path is everything before the last two fields: a file name may hold a tab.
settle_check() { # earlier-listing later-listing ready-file
  : > "$3"
  awk -F"$TAB" -v now="$(date +%s)" -v ready="$3" '
    FILENAME == ARGV[1] { seen[$0] = 1; next }
    {
      path = $0; sub(/\t[^\t]*\t[^\t]*$/, "", path)
      name = path; sub(/.*\//, "", name)
      if (name ~ /\.(download|crdownload|part|partial)$/) { if (now - $NF < 300) arriving++; next }
      if (name ~ /^(\.|Icon|~\$)/ || name == "README.md" || name ~ /\.tmp$/) next
      files++
      if ($0 in seen) print path > ready; else arriving++
    }
    END { printf "%d %d\n", files, arriving }' "$1" "$2"
  LC_ALL=C sort -o "$3" "$3"
}

# Waits until every queued file is ready, or SETTLE_MAX_SECONDS have passed; the live line
# says how many files there are, how many are still arriving, and how much longer it
# waits at most. Leaves the ready paths in $READY_FILE: this run reads only those.
wait_for_settle() {
  local waited=0 a="$STATE_DIR/settle.a" b="$STATE_DIR/settle.b" facts files arriving ready since
  since=$(date '+%Y-%m-%dT%H:%M:%S')
  queue_stats > "$a"
  facts=$(settle_check /dev/null "$a" "$READY_FILE"); files=${facts% *}   # the files there are
  nowp settle detail="checking that every upload and copy has finished: $(thousands "$files") file(s) in the queue, at most ${SETTLE_MAX_SECONDS}s" \
    since="$since" files:="$files"
  while :; do
    sleep "$SETTLE_SECONDS"; waited=$((waited + SETTLE_SECONDS))
    queue_stats > "$b"
    facts=$(settle_check "$a" "$b" "$READY_FILE")
    files=${facts% *}; arriving=${facts#* }
    [ "$arriving" = 0 ] && return 0
    ready=$(wc -l < "$READY_FILE" | tr -d ' ')
    if [ "$waited" -ge "$SETTLE_MAX_SECONDS" ]; then
      log "$arriving file(s) still arriving after ${waited}s; reading the $ready that are ready, the rest in the next run"
      return 0
    fi
    nowp settle detail="checking that every upload and copy has finished: $(thousands "$ready") file(s) ready, $(thousands "$arriving") still arriving, at most $((SETTLE_MAX_SECONDS - waited))s more" \
      since="$since" files:="$files" ready:="$ready" arriving:="$arriving" maxWait:="$((SETTLE_MAX_SECONDS - waited))"
    mv "$b" "$a"
  done
}

# The Markdown files among those listed on stdin that are Update Packets (their first 400
# bytes hold a packet header), in one process however many there are.
packets_among() {
  "$PY" -c '
import re, sys
head = re.compile(rb"^## \[[0-9]{4}-[0-9]{2}-[0-9]{2}\] *update *\|", re.M)
for line in sys.stdin:
    p = line.rstrip("\n")
    try:
        with open(p, "rb") as f:
            if head.search(f.read(400)):
                print(p)
    except OSError:
        pass
' 2>> "$LOG_FILE"
}

route_to() { # file dest-dir: the move is recorded in $ROUTED for one event each, later
  local f="$1" d="$2" b
  b=${f##*/}
  if [ -e "$d/$b" ]; then
    # The same file dropped into both folders is processed once, not twice.
    if cmp -s "$f" "$d/$b"; then
      rm -f "$f" && log "dropped $f: an identical copy is already in $d"
      return 0
    fi
    b="$(date +%Y%m%d%H%M%S)-$b"
  fi
  mv "$f" "$d/$b" && log "routed $f -> $d/$b" && printf '%s\t%s\n' "$f" "$d/$b" >> "$ROUTED"
}

route_misfiled() {
  local f
  ROUTED="$STATE_DIR/routed.tsv"; : > "$ROUTED"
  mkdir -p raw/_intake raw/inbox
  # A document dropped into the Inbox belongs in Intake.
  list_pending raw/inbox | while IFS= read -r f; do
    case "$f" in *.md|*.markdown) continue ;; esac
    route_to "$f" raw/_intake
  done
  # A packet dropped into Intake belongs in the Inbox.
  list_pending raw/_intake | grep '\.md$' | packets_among | while IFS= read -r f; do
    route_to "$f" raw/inbox
  done
  [ -s "$ROUTED" ] && evs routed --key file --key to < "$ROUTED"
  return 0
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
  budget=${SPEND_CAP:-5}
  if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    log "claude not found on PATH"
    [ -n "${WHY_FILE:-}" ] && echo "the claude command is not installed" > "$WHY_FILE"
    return 127
  fi
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
  # Why it could not run, for the notice: Claude's own words, from its stream or the log.
  [ -n "${WHY_FILE:-}" ] && { tail -n 40 "$stream"; tail -n 40 "$LOG_FILE"; } 2>/dev/null \
    | grep -i -m 1 -E 'authenticat|api key|log ?in|oauth|unauthori|limit|overloaded|credit|api error|econn|enotfound|etimedout|timed out|connection (error|refused)|reach the api|server error|service unavailable|bad gateway' > "$WHY_FILE"
  return 1
}

# The notice for Claude being unable to run, from what it said: a sign-in problem is
# named as one only when that is what it was, and so is a Mac that is offline.
claude_problem() { # reason
  local why low
  why=$(printf '%s' "${1#Claude could not run: }" | tr '\n\t' '  ' | cut -c1-160)
  low=$(printf '%s' "$why" | tr 'A-Z' 'a-z')
  case "$low" in
    *"failed to authenticate"*|*"invalid api key"*|*"api key is invalid"*|*"not logged in"*|*"/login"*|*"oauth token"*|*unauthori*|*authentication*)
      echo "Claude could not run: it is not signed in. Check your Anthropic sign-in (run the installer again), then press Process now on the Upload page." ;;
    *"usage limit"*|*"limit reached"*|*"rate limit"*|*rate_limit*|*overloaded*|*"credit balance"*)
      echo "Claude could not run: $why. Nothing is lost: the documents wait, and the next run tries again." ;;
    *"api error: connection"*|*"connection error"*|*"connection refused"*|*econnrefused*|*enotfound*|*econnreset*|*etimedout*|*eai_again*|*"reach the api server"*|*"unable to connect"*|*"socket hang up"*|*"network error"*|*"fetch failed"*|*"request timed out"*)
      echo "Claude could not be reached: this Mac seems to be offline, or its connection to Anthropic failed ($why). Nothing is lost: the documents wait, and the next run tries again." ;;
    *"internal server error"*|*"service unavailable"*|*"bad gateway"*|*"gateway timeout"*|*"api error: 5"*|*"documents in a row"*)
      echo "Claude could not run: Anthropic's service is having trouble ($why). Nothing is lost: the documents wait, and the next run tries again." ;;
    *"not installed"*)
      echo "Claude could not run: the claude command is not installed. Run the installer again." ;;
    *)
      echo "Claude could not run${why:+ ($why)}. The documents wait for the next run; see ~/Library/Logs/wiki-starter/runner.log." ;;
  esac
}

# ---------------------------------------------------------- local model ---------
# One run of scripts/local_engine.py (one batch, or the packets). Returns 0 done (1 from
# the engine means some files failed: they stay queued and count a try, like any file);
# 2 the model could not run; 4 stopped by the watchdog; 5 it died by itself (a signal: a
# native crash, which on a Mac shows "Python quit unexpectedly"); 6 something else stopped
# it (SIGTERM from outside). "No progress" is the progress marker (.wiki-engine/state/
# progress, touched by scripts/wiki_events.py only when something actually moves: an
# event, a new step, a new count, more streamed words; conversion running ahead of the
# reading touches a marker of its own, convert-progress) not changing for
# LOCAL_STALL_SECONDS: a slow Mac is never cut off, a hung model or Claude call is, even
# while the live line repeats that it is still reading. Checked every second, so a
# finished batch frees its turn at once. A Mac that slept is not charged for it: when the
# clock jumps past the loop's step, the watchdog starts over.
run_local() { # local_engine.py run arguments
  local pid waited=0 rc idle stopped=0 now last since step left
  log "local model: start ($*)"
  touch "$PROGRESS"
  "$PY" scripts/local_engine.py run "$@" >> "$LOG_FILE" 2>&1 < /dev/null &
  pid=$!
  last=$(date +%s); since=$last   # since: the last sign of progress (or waking up)
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    now=$(date +%s); step=$((now - last)); last=$now
    if [ "$step" -gt 30 ]; then
      log "local model: the clock jumped ${step}s (the Mac was asleep); the watchdog starts over"
      since=$now
    else
      waited=$((waited + step))
    fi
    step=$(mtime "$PROGRESS"); [ "$step" -gt "$since" ] && since=$step
    idle=$((now - since))
    if [ "$idle" -ge "$LOCAL_STALL_SECONDS" ] || [ "$waited" -ge "$LOCAL_TIMEOUT_SECONDS" ]; then
      log "local model: watchdog stopped it after ${waited}s (no progress for ${idle}s)"
      STALLED_ON=$(now_file)
      kill "$pid" 2>/dev/null
      left=40   # time to stop the model server it started
      while [ "$left" -gt 0 ] && kill -0 "$pid" 2>/dev/null; do sleep 1; left=$((left - 1)); done
      pkill -P "$pid" 2>/dev/null   # the model server, if it outlived its parent
      kill -9 "$pid" 2>/dev/null
      stopped=1
      break
    fi
  done
  wait "$pid" 2>/dev/null; rc=$?
  log "local model: exit $rc after ${waited}s"
  [ "$stopped" = 1 ] && return 4
  if [ "$rc" = 143 ]; then
    log "local model: it was stopped from outside (SIGTERM)"
    return 6
  fi
  if [ "$rc" -ge 128 ]; then
    log "local model: it stopped unexpectedly (signal $((rc - 128)))"
    return 5
  fi
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
    b=${p##*/}
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
append_ledger() { # before-snapshot after-snapshot run-id: one line per newly filed source
  local now
  LC_ALL=C comm -13 "$1" "$2" | grep -q . || return 0
  now=$(date '+%Y-%m-%d %H:%M')
  mkdir -p archive
  [ -f "$LEDGER" ] || echo "ingested,path,run" > "$LEDGER"
  # The path quoted as a CSV field: in double quotes, a double quote doubled.
  LC_ALL=C comm -13 "$1" "$2" | sed '/^$/d' \
    | awk -v now="$now" -v run="$3" '{ gsub(/"/, "\"\""); printf "%s,\"%s\",%s\n", now, $0, run }' >> "$LEDGER"
}

# What actually happened to each file this run started with, read from the folders: the
# authority for the Upload page when Claude's own action stream missed a step (or reported
# it wrongly). A document that left the queue is matched by its name to a newly filed
# source, through a table of names built once (a search per document took minutes for a
# few thousand), and the events are written in one go.
confirm_done() { # before-snapshot after-snapshot
  local p new="$STATE_DIR/confirm.new" gone="$STATE_DIR/confirm.gone"
  LC_ALL=C comm -13 "$1" "$2" > "$new"
  printf '%s\n' "$DOCS_BEFORE" | sed '/^$/d' | while IFS= read -r p; do
    [ -e "$p" ] || printf '%s\n' "$p"
  done > "$gone"
  awk 'FILENAME == ARGV[1] { b = $0; sub(/.*\//, "", b); if (!(b in to)) to[b] = $0; next }
       { b = $0; sub(/.*\//, "", b); if (b in to) printf "%s\t%s\n", $0, to[b] }' "$new" "$gone" \
    | evs filed --key file --key to confirmed:=true
  printf '%s\n' "$PACKETS_BEFORE" | sed '/^$/d' | while IFS= read -r p; do
    [ -e "$p" ] && continue
    [ -f "archive/inbox/${p##*/}" ] && printf '%s\tarchive/inbox/%s\n' "$p" "${p##*/}"
  done | evs applied --key file --key to confirmed:=true
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
  # Only folders that exist: git refuses the whole list if one is missing.
  local p paths=""
  for p in wiki index.md log.md archive generated review actions; do [ -e "$p" ] && paths="$paths $p"; done
  git add -A -- $paths >> "$LOG_FILE" 2>&1 || true
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
  local rc
  "$NODE_BIN" quartz/bootstrap-cli.mjs build -d wiki -o "$next" >> "$LOG_FILE" 2>&1 &
  wait_at_most $! "$BUILD_TIMEOUT_SECONDS"; rc=$?
  [ "$rc" = 124 ] && log "site build stopped: it took longer than ${BUILD_TIMEOUT_SECONDS}s"
  if [ "$rc" = 0 ] && [ -f "$next/index.html" ]; then
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
# A run that died without cleaning up (a crash, SIGKILL, a power cut) left the live line on
# its last step: the activity log says so, then the live line starts afresh.
PREV_NOW=$("$PY" -c '
import json, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception:
    r = {}
print((r.get("phase") or "idle") + "\t" + (r.get("run") or "an earlier run"))' "$STATE_DIR/now.json" 2>/dev/null)
case "${PREV_NOW:-idle}" in
  idle*) ;;
  *) ev interrupted run="${PREV_NOW#*$TAB}" phase="${PREV_NOW%%$TAB*}" \
       msg="The last run (${PREV_NOW#*$TAB}) stopped unexpectedly at the ${PREV_NOW%%$TAB*} step; what it left is read again now"
     log "the last run (${PREV_NOW#*$TAB}) stopped unexpectedly at the ${PREV_NOW%%$TAB*} step"
     nowp idle ;;
esac
# Wikis from before the performance modes move to them once (it does nothing after that).
"$PY" scripts/wiki_settings.py migrate >> "$LOG_FILE" 2>&1 || log "the settings could not be moved to the performance modes"
refresh_starter_pages

if [ "$REBUILD_ONLY" = 1 ]; then
  remove_old_desktop_links
  build_site; exit $?
fi

# Another run, at once, in place of this one (step 10): it takes the lock again.
rerun_now() {
  [ -n "${AWAKE:-}" ] && kill "$AWAKE" 2>/dev/null
  nowp idle
  release_lock
  trap - EXIT
  export WIKI_RERUN_DEPTH=$(( ${WIKI_RERUN_DEPTH:-0} + 1 ))
  exec /bin/bash "$WIKI_DIR/scripts/wiki_runner.sh"
}

RUN_ID="run-$(date +%Y%m%d-%H%M%S)"
export WIKI_RUN_ID="$RUN_ID"
mkdir -p raw/inbox raw/_intake archive/inbox review/open review/done actions/open actions/done
route_misfiled

if [ -z "$(list_pending raw/_intake)$(list_pending raw/inbox)" ]; then
  # Nothing to do. Make sure there is a site to look at, then stop quietly.
  [ -f public/index.html ] || build_site
  exit 0
fi

# Keep the Mac awake (not its screen) while this run works; it ends with the run.
AWAKE=""
if command -v caffeinate >/dev/null 2>&1; then caffeinate -i -w $$ >/dev/null 2>&1 & AWAKE=$!; fi

READY_FILE="$STATE_DIR/ready.txt"; rm -f "$READY_FILE"
if [ "$SETTLE" = 1 ]; then
  ev waiting msg="Waiting for files to finish copying"
  wait_for_settle
fi
# From here on the live line says what is being prepared, never "settling".
nowp prepare detail="getting the documents ready"
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
if [ -f "$READY_FILE" ]; then
  # Only what had settled: a file still arriving (or moved here since) waits for the run
  # that starts as this one ends.
  WAITING_ALL=$(( $(count_lines "$DOCS_BEFORE") + $(count_lines "$PACKETS_BEFORE") ))
  DOCS_BEFORE=$(printf '%s\n' "$DOCS_BEFORE" | sed '/^$/d' | LC_ALL=C comm -12 - "$READY_FILE")
  PACKETS_BEFORE=$(printf '%s\n' "$PACKETS_BEFORE" | sed '/^$/d' | LC_ALL=C comm -12 - "$READY_FILE")
fi
# The packets this run applies: those that had settled. One still being written (an editor
# or a sync client saving it) waits for the run that starts as this one ends.
PACKETS_READY="$STATE_DIR/packets.ready"
printf '%s\n' "$PACKETS_BEFORE" | sed '/^$/d' | LC_ALL=C sort > "$PACKETS_READY"
ready_packets() { list_packets | LC_ALL=C sort | LC_ALL=C comm -12 - "$PACKETS_READY"; }
N_DOCS=$(count_lines "$DOCS_BEFORE")
N_PACKETS=$(count_lines "$PACKETS_BEFORE")
if [ $((N_DOCS + N_PACKETS)) = 0 ]; then
  # Nothing has settled yet: look again in a moment, in a fresh run.
  log "nothing has finished arriving yet"
  [ "${WIKI_RERUN_DEPTH:-0}" -lt "$MAX_RERUNS" ] && rerun_now
  exit 0
fi
WHAT="$(thousands "$N_DOCS") document(s)"; [ "$N_PACKETS" -gt 0 ] && WHAT="$WHAT and $(thousands "$N_PACKETS") packet(s)"
nowp prepare detail="getting $WHAT ready" n:=0 of:="$((N_DOCS + N_PACKETS))"
# Documents per batch: the performance mode's figure for this way of reading (fast,
# classic or the model on this Mac), or the owner's own.
BATCH_SIZE=$(setting docsPerBatch)
case "$BATCH_SIZE" in
  ''|*[!0-9]*|0) if [ "$ENGINE" = local ]; then BATCH_SIZE=40; elif [ "$PIPELINE" = fast ]; then BATCH_SIZE=100; else BATCH_SIZE=10; fi ;;
esac
SPEND_CAP=$(setting maxSpendPerBatchUsd)
case "$SPEND_CAP" in ''|*[!0-9.]*) SPEND_CAP=5 ;; esac
BATCH_DIR="$STATE_DIR/batches"; TRIED="$STATE_DIR/tried.txt"; CAPPED="$STATE_DIR/capped.txt"
SUSPECTS="$STATE_DIR/suspects.txt"   # being read when the reader died: kept across runs
rm -rf "$BATCH_DIR"; mkdir -p "$BATCH_DIR"; : > "$TRIED"; : > "$CAPPED"; touch "$SUSPECTS"
[ -n "$DOCS_BEFORE" ] && printf '%s\n' "$DOCS_BEFORE" | split -a 4 -l "$BATCH_SIZE" - "$BATCH_DIR/batch."
N_BATCHES=$(ls "$BATCH_DIR" | wc -l | tr -d ' ')
log "$RUN_ID: $N_DOCS document(s), $N_PACKETS packet(s) waiting${WAITING_ALL:+ ($((WAITING_ALL - N_DOCS - N_PACKETS)) more still arriving)}"
# How Claude is paid for, so the Upload page can say what its cost estimate means.
AUTH=login
if [ "$ENGINE" = local ]; then AUTH=local
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then AUTH=api
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then AUTH=plan; fi
ev run-start docs:="$N_DOCS" packets:="$N_PACKETS" auth="$AUTH"
printf '%s\n%s\n' "$DOCS_BEFORE" "$PACKETS_BEFORE" | sed '/^$/d' | evs queued --key file --progress
BATCH_NOTE=""; [ "$N_BATCHES" -gt 1 ] && BATCH_NOTE=" in $N_BATCHES batches"
# Announced once: not again for a run that follows on from this one, nor after a run that
# had to stop (a notice about that went out; one every 15 minutes would be noise).
if [ "${WIKI_RERUN_DEPTH:-0}" = 0 ] && [ -z "$(cat "$STATE_DIR/last-stop" 2>/dev/null)" ]; then
  notify "Wiki is working" "Reading $N_DOCS document(s)$BATCH_NOTE and $N_PACKETS packet(s). This can take a while; another notice follows when it is done."
fi

SNAP_BEFORE="$STATE_DIR/snap.before"; SNAP_AFTER="$STATE_DIR/snap.after"
snapshot_filed_sources > "$SNAP_BEFORE"
ARCHIVED_BEFORE=$(find archive/inbox -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')

# Documents, in batches. Conversion runs ahead in the background, so reading never
# waits for it. Claude reads up to LANES batches at once (parallelBatches, the reading
# speed: 6 unless the owner chose less); the first batch runs alone, so a bad sign-in
# fails after one call and the pages every batch shares (the business, the project)
# exist before several sessions edit them. The model on this Mac, and the fast pipeline
# (whose parallelism is inside each batch: Claude reads many of its documents at once),
# read one batch at a time.
# A batch that ran (even one stopped at Claude's cap) counts as a try for its files; a
# batch that never ran counts nothing. In the fast pipeline, the documents a batch never
# put to Claude because it reached its spending cap count nothing either, and are read
# by the run that starts as this one ends. Claude or the model failing outright stops
# the run; so does a hang, which counts a try only for the file it hung on, and a crash
# of the reader, which counts a try only for the files it was reading (the next run
# reads those first, one at a time, so a second crash is pinned on the right one).
LANES=1
if [ "$ENGINE" != local ] && [ "$PIPELINE" != fast ]; then
  LANES=$(setting parallelBatches)
  case "$LANES" in ''|*[!0-9]*|0) LANES=6 ;; esac
  [ "$LANES" -gt 6 ] && LANES=6
fi
LANE_DIR="$STATE_DIR/lanes"; rm -rf "$LANE_DIR"; mkdir -p "$LANE_DIR"
# Each session keeps its own stream; the last run's go now (and the single stream of
# engines before this one).
rm -f "$STATE_DIR"/claude-stream*.jsonl "$STATE_DIR"/claude-current-*
BATCHES=$(ls "$BATCH_DIR" | tr '\n' ' ')
BUILD_EVERY_SECONDS=180
CLAUDE_OK=1; STOP=""; STALLED_ON=""; CRASHED=0; WHY=""; WHY_FILE=""

# One conversion process for a list, watched (scripts/wiki_events.py convert-stream
# --list): a crash or a hang marks the file it was on unreadable and the rest goes on.
# A converter that cannot work at all (it dies before reaching any file, or on file after
# file) leaves converter-broken: the run stops there, and no document counts a try.
convert_list() { # list-file start [--quiet]
  local rc
  "$PY" scripts/wiki_events.py convert-stream --list "$1" --total "$N_DOCS" --start "$2" \
    --stall "$CONVERT_STALL_SECONDS" ${3:-} >> "$LOG_FILE" 2>&1; rc=$?
  case "$rc" in
    0) ;;
    3) log "the document converter is not working"; : > "$LANE_DIR/converter-broken" ;;
    *) log "to_markdown reported errors" ;;
  esac
}

# The fast pipeline reads faster than one process can convert scans with OCR: each of its
# batches is converted by CONVERT_JOBS processes at once (half the Mac's cores, and one
# per 4 GB of memory, at most 8; fast.convertJobs in wiki.config.json overrides). Each OCR
# gets its share of the cores (WIKI_CONVERT_JOBS, read by scripts/to_markdown.py).
memory_gb() {
  local b
  b=$(sysctl -n hw.memsize 2>/dev/null)
  if [ -n "$b" ]; then echo $(( b / 1073741824 )); return; fi
  b=$(awk '/^MemTotal:/ { print $2 }' /proc/meminfo 2>/dev/null)   # kB
  echo $(( ${b:-0} / 1048576 ))
}
CONVERT_JOBS=1
if [ "$PIPELINE" = fast ]; then
  CONVERT_JOBS=$(config_value fast.convertJobs "")
  case "$CONVERT_JOBS" in
    ''|*[!0-9]*|0)
      CONVERT_JOBS=$(( $(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 2) / 2 ))
      RAM_GB=$(memory_gb)
      if [ "$RAM_GB" -gt 0 ] && [ "$CONVERT_JOBS" -gt $(( RAM_GB / 4 )) ]; then
        log "converting $(( RAM_GB / 4 > 1 ? RAM_GB / 4 : 1 )) at a time, not $CONVERT_JOBS: this Mac has ${RAM_GB} GB of memory"
        CONVERT_JOBS=$(( RAM_GB / 4 ))
      fi ;;
  esac
  [ "$CONVERT_JOBS" -lt 1 ] && CONVERT_JOBS=1
  [ "$CONVERT_JOBS" -gt 8 ] && CONVERT_JOBS=8
fi
export WIKI_CONVERT_JOBS="$CONVERT_JOBS"
# Conversion keeps at most AHEAD batches ahead of the reading: a run that stops early has
# not spent hours on OCR for documents it will not reach.
AHEAD=$((LANES + 1))

convert_batches() { # every batch in order, each marked ready once converted
  local b n started=0 quiet="" part pids at i=0 gate
  for b in $BATCHES; do
    i=$((i + 1))
    if [ "$i" -gt "$AHEAD" ]; then   # wait until the reading has caught up
      gate=$(printf '%s\n' $BATCHES | sed -n "$((i - AHEAD))p")
      while [ ! -f "$LANE_DIR/$gate.started" ] && [ ! -f "$LANE_DIR/stop" ]; do sleep 1; done
    fi
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
    [ -f "$LANE_DIR/converter-broken" ] && break   # the run stops: nothing more to convert
    started=$((started + n))
    : > "$LANE_DIR/$b.ready"
    quiet=--quiet   # from here on the live line belongs to the reading
    # ... and the progress marker too: conversion running ahead touches convert-progress
    # (scripts/wiki_events.py), so a reader that hangs is seen, however much is converted.
    export WIKI_CONVERT_AHEAD=1
  done
}

read_batch() { # batch k: runs in the background; leaves its outcome in the lane folder
  local b="$1" k="$2" n rc
  n=$(wc -l < "$BATCH_DIR/$b" | tr -d ' ')
  [ "$N_BATCHES" -gt 1 ] && export WIKI_BATCH="$k/$N_BATCHES"
  log "batch $k of $N_BATCHES: $n document(s)"
  ev batch n:="$k" of:="$N_BATCHES" docs:="$n"
  STALLED_ON=""
  # local_engine.py says which documents a spending cap left unread (and which it tried)
  # and keeps the ones it is reading at each moment, in case it dies.
  # If Claude or the model cannot run, $b.why says why (for the notice).
  WHY_FILE="$LANE_DIR/$b.why"
  if [ "$ENGINE" = local ]; then
    run_local --docs "$BATCH_DIR/$b" --tried "$LANE_DIR/$b.tried" --capped "$LANE_DIR/$b.capped" \
      --inflight "$LANE_DIR/$b.inflight" --why "$WHY_FILE"; rc=$?
  elif [ "$PIPELINE" = fast ]; then
    if command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
      # Documents it was reading when it died in an earlier run go first, one at a time.
      run_local --docs "$BATCH_DIR/$b" --reader claude --tried "$LANE_DIR/$b.tried" \
        --capped "$LANE_DIR/$b.capped" --inflight "$LANE_DIR/$b.inflight" --alone "$SUSPECTS" \
        --why "$WHY_FILE"; rc=$?
    else log "claude not found on PATH"; echo "the claude command is not installed" > "$WHY_FILE"; rc=2; fi
  else
    batch_prompt "$BATCH_DIR/$b" > "$LANE_DIR/$b.prompt.md"
    run_claude "intake#$k" "$LANE_DIR/$b.prompt.md" 100 "$BATCH_DIR/$b"; rc=$?
  fi
  printf '%s' "$STALLED_ON" > "$LANE_DIR/$b.stalled"
  printf '%s' "$rc" > "$LANE_DIR/$b.rc"
}

finish_batch() { # batch: count its tries; a failure stops new batches
  local b="$1" rc stalled tried="$LANE_DIR/$1.tried" capped="$LANE_DIR/$1.capped" inflight="$LANE_DIR/$1.inflight"
  rc=$(cat "$LANE_DIR/$b.rc" 2>/dev/null); stalled=$(cat "$LANE_DIR/$b.stalled" 2>/dev/null)
  case "$rc" in
    0|3) if [ -f "$capped" ]; then
           # Left unread by the spending cap, as local_engine.py names them: no try; the
           # next run reads them. Every other document of the batch counts a try, also
           # one the reader never saw: never taken for one the cap left, read again and
           # again, it is set aside in the end like any document that is not read.
           cat "$capped" >> "$CAPPED"
           LC_ALL=C comm -23 <(LC_ALL=C sort "$BATCH_DIR/$b") <(LC_ALL=C sort "$capped") >> "$TRIED"
         else cat "$BATCH_DIR/$b" >> "$TRIED"; fi ;;
    4) # A hang: a try for the file it hung on; with Claude reading many at once, for the
       # ones it was reading (they are read one at a time next run); else the batch.
       if [ -n "$stalled" ]; then printf '%s\n' "$stalled" >> "$TRIED"; STALLED_ON="$stalled"
       elif [ -s "$inflight" ]; then
         cat "$inflight" >> "$TRIED"; cat "$inflight" >> "$SUSPECTS"; STALLED_ON=$(head -1 "$inflight")
       else cat "$BATCH_DIR/$b" >> "$TRIED"; fi
       [ "$STOP" = engine ] || STOP=stalled; CLAUDE_OK=0 ;;
    6) [ "$STOP" = engine ] || STOP=stopped; CLAUDE_OK=0 ;;   # stopped from outside: no tries
    5) # The reader died. Like a hang: a try for the documents it was reading (or, if it
       # died after the reading, for what it tried; if it is not known, the whole batch),
       # and no new batch this run. Those it was reading are read one at a time next
       # run (suspects), so a second crash counts against the one at fault alone.
       if [ -f "$tried" ]; then cat "$tried" >> "$TRIED"
       elif [ -s "$inflight" ]; then
         cat "$inflight" >> "$TRIED"; cat "$inflight" >> "$SUSPECTS"; STALLED_ON=$(head -1 "$inflight")
       else cat "$BATCH_DIR/$b" >> "$TRIED"; fi
       CRASHED=1; [ "$STOP" = engine ] || STOP=stalled; CLAUDE_OK=0 ;;
    *) STOP=engine; CLAUDE_OK=0; WHY=$(head -c 300 "$LANE_DIR/$b.why" 2>/dev/null) ;;
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
  # A background process seen to have exited is collected and forgotten at once: its
  # number is free, and may soon be another program's, which must never be stopped.
  if [ -n "$CONVERTER" ] && ! kill -0 "$CONVERTER" 2>/dev/null; then wait "$CONVERTER" 2>/dev/null; CONVERTER=""; fi
  if [ -n "$BUILDER" ] && ! kill -0 "$BUILDER" 2>/dev/null; then wait "$BUILDER" 2>/dev/null; BUILDER=""; fi
  if [ -f "$LANE_DIR/converter-broken" ] && [ "$CLAUDE_OK" = 1 ]; then
    STOP=converter; CLAUDE_OK=0   # nothing new starts; what is reading finishes
  fi
  busy=$(printf '%s\n' $running | sed '/^$/d' | wc -l | tr -d ' ')
  limit=$LANES; [ "$first_done" = 1 ] || limit=1
  while [ "$CLAUDE_OK" = 1 ] && [ -n "$(printf '%s' "$queue" | tr -d ' ')" ] && [ "$busy" -lt "$limit" ]; do
    b=$(printf '%s\n' $queue | head -1)
    if [ ! -f "$LANE_DIR/$b.ready" ]; then
      [ -n "$CONVERTER" ] && kill -0 "$CONVERTER" 2>/dev/null && break
      : > "$LANE_DIR/$b.ready"   # the converter is gone: Claude notes any missing text
    fi
    queue=$(printf '%s\n' $queue | sed 1d | tr '\n' ' ')
    k=$((k + 1))
    : > "$LANE_DIR/$b.started"
    read_batch "$b" "$k" &
    running="$running $!:$b"; busy=$((busy + 1))
  done
  if [ -z "$(printf '%s' "$running" | tr -d ' ')" ]; then
    [ "$CLAUDE_OK" = 1 ] && [ -n "$(printf '%s' "$queue" | tr -d ' ')" ] || break
  fi
  sleep 1
done
: > "$LANE_DIR/stop"
# A run that stops early does not wait for conversions it will not read.
[ -n "$STOP" ] && [ -n "$CONVERTER" ] && kill_tree "$CONVERTER"
[ -n "$CONVERTER" ] && wait "$CONVERTER" 2>/dev/null; CONVERTER=""
[ -n "$BUILDER" ] && wait_at_most "$BUILDER" "$BUILD_TIMEOUT_SECONDS"; BUILDER=""
unset WIKI_BATCH

# Update Packets, after the documents.
if [ "$ENGINE" = local ]; then
  MAX_INGEST_ROUNDS=0   # the model on this Mac applies them in one call
  LOCAL_PACKETS=$(ready_packets)
  if [ "$CLAUDE_OK" = 1 ] && [ -n "$LOCAL_PACKETS" ]; then
    printf '%s\n' "$LOCAL_PACKETS" > "$LANE_DIR/packets.list"
    run_local --packets --packet-list "$LANE_DIR/packets.list" --inflight "$LANE_DIR/packets.inflight" \
      --why "$LANE_DIR/packets.why"; rc=$?
    case "$rc" in
      0) printf '%s\n' "$LOCAL_PACKETS" >> "$TRIED" ;;
      4) [ -n "$STALLED_ON" ] && printf '%s\n' "$STALLED_ON" >> "$TRIED"; STOP=stalled; CLAUDE_OK=0 ;;
      5) if [ -s "$LANE_DIR/packets.inflight" ]; then
           cat "$LANE_DIR/packets.inflight" >> "$TRIED"; STALLED_ON=$(head -1 "$LANE_DIR/packets.inflight")
         else printf '%s\n' "$LOCAL_PACKETS" >> "$TRIED"; fi
         CRASHED=1; STOP=stalled; CLAUDE_OK=0 ;;
      6) STOP=stopped; CLAUDE_OK=0 ;;
      *) STOP=engine; CLAUDE_OK=0; WHY=$(head -c 300 "$LANE_DIR/packets.why" 2>/dev/null) ;;
    esac
  fi
fi

round=0
while [ "$CLAUDE_OK" = 1 ] && [ "$round" -lt "$MAX_INGEST_ROUNDS" ]; do
  # Five at a time (engine/prompts/ingest.md), oldest first, of those that had settled;
  # Claude is told which, and to leave every other file in raw/inbox/ alone.
  ROUND_PACKETS=$(ready_packets | head -5)
  [ -n "$ROUND_PACKETS" ] || break
  round=$((round + 1))
  {
    cat engine/prompts/ingest.md; business_note
    printf '\nApply only these packets, and leave every other file in raw/inbox/ untouched (they are applied later):\n\n'
    printf '%s\n' "$ROUND_PACKETS" | sed 's/^/- /'
  } > "$LANE_DIR/ingest.prompt.md"
  WHY_FILE="$LANE_DIR/ingest.why"
  run_claude "ingest#$round" "$LANE_DIR/ingest.prompt.md" 60; rc=$?
  case "$rc" in
    0|3) printf '%s\n' "$ROUND_PACKETS" >> "$TRIED" ;;
    4) printf '%s\n' "$ROUND_PACKETS" >> "$TRIED"; STOP=stalled; CLAUDE_OK=0; break ;;
    *) STOP=engine; CLAUDE_OK=0; WHY=$(head -c 300 "$WHY_FILE" 2>/dev/null); break ;;
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
# What still waits: the queues as bump_attempts listed them, less what was just parked.
PENDING_NOW=$(while IFS= read -r p; do [ -f "$p" ] && printf '%s\n' "$p"; done < "$STATE_DIR/pending.now")
printf '%s\n' "$PENDING_NOW" | sed '/^$/d' | LC_ALL=C sort \
  | LC_ALL=C comm -12 - <(sed '/^$/d' "$TRIED" | LC_ALL=C sort -u) | evs retry --key file
LEFT=$(printf '%s\n' "$PENDING_NOW" | grep -c '^raw/_intake/')
CAP_LEFT=$(printf '%s\n' "$PENDING_NOW" | sed '/^$/d' | LC_ALL=C sort \
  | LC_ALL=C comm -12 - <(sed '/^$/d' "$CAPPED" | LC_ALL=C sort -u) | wc -l | tr -d ' ')
# Suspects stay suspects only while they wait (one filed or parked is cleared).
printf '%s\n' "$PENDING_NOW" | sed '/^$/d' | LC_ALL=C sort \
  | LC_ALL=C comm -12 - <(sed '/^$/d' "$SUSPECTS" | LC_ALL=C sort -u) > "$SUSPECTS.new"
mv "$SUSPECTS.new" "$SUSPECTS"
ON=""; [ -n "$STALLED_ON" ] && ON=" while reading ${STALLED_ON##*/}"
case "$STOP:$ENGINE:$CRASHED" in
  engine:local:*) notify_error "The local model could not run. See ~/Library/Logs/wiki-starter/runner.log, or run the installer again to repair it." ;;
  engine:*) notify_error "$(claude_problem "$WHY")" ;;
  converter:*) notify_error "The document converter is not working: it stops before it can convert the documents, so nothing more was read and nothing counts against them. See ~/Library/Logs/wiki-starter/runner.log; running the installer again usually repairs it." ;;
  stopped:*) notify_error "Reading was stopped by something outside the wiki. The $LEFT document(s) still waiting will be read on the next run." ;;
  stalled:local:1) notify_error "The model on this Mac stopped unexpectedly$ON. The $LEFT document(s) still waiting will be tried again on the next run." ;;
  stalled:*:1) notify_error "The reader stopped unexpectedly$ON. The $LEFT document(s) still waiting will be tried again on the next run." ;;
  stalled:local:*) notify_error "The model on this Mac stopped responding$ON. The $LEFT document(s) still waiting will be read on the next run." ;;
  stalled:*) notify_error "Claude stopped responding during a batch. The $LEFT document(s) still waiting will be read on the next run." ;;
esac
printf '%s' "$STOP" > "$STATE_DIR/last-stop"   # the next run's working notice depends on it

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

# Files dropped while this run was busy (or still arriving when it started) would
# otherwise wait for the 15-minute timer: the folder watcher fired, found this run holding
# the lock, and gave up. Documents a spending cap left unread would wait the same. Start
# another run now (at most MAX_RERUNS in a row, and never when Claude itself could not run).
ARRIVED=$( { list_pending raw/_intake; list_pending raw/inbox; } | LC_ALL=C sort \
  | LC_ALL=C comm -23 - <(printf '%s\n%s\n' "$DOCS_BEFORE" "$PACKETS_BEFORE" | sed '/^$/d' | LC_ALL=C sort) )
[ "$CAP_LEFT" -gt 0 ] && log "$CAP_LEFT document(s) left unread by the spending cap (no try counted)"
if [ "$CLAUDE_OK" = 1 ] && { [ -n "$ARRIVED" ] || [ "$CAP_LEFT" -gt 0 ]; } \
   && [ "${WIKI_RERUN_DEPTH:-0}" -lt "$MAX_RERUNS" ]; then
  if [ -n "$ARRIVED" ]; then
    log "$(count_lines "$ARRIVED") file(s) arrived during this run; starting another run"
    ev rerun count:="$(count_lines "$ARRIVED")"
  fi
  if [ "$CAP_LEFT" -gt 0 ]; then
    log "starting another run for the $CAP_LEFT document(s) the spending cap left"
    ev cap-rerun count:="$CAP_LEFT" msg="$CAP_LEFT document(s) the spending cap left unread are read now, in another run"
  fi
  rerun_now
fi
exit 0
