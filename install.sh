#!/bin/bash
# wiki-starter installer — sets up an LLM-maintained wiki that lives on this Mac.
#
#   curl -fsSL https://raw.githubusercontent.com/arkceo/wiki-starter/main/install.sh | bash
#
# What it does (each step checks first, so running it again is safe and resumes):
#   1. checks this is a Mac you can administer;
#   2. asks for the wiki's name, and who should read your documents: Claude (needs an
#      Anthropic sign-in) or a model that runs on this Mac (nothing leaves it, no
#      account; a one-off download of a few GB);
#   3. installs Homebrew (asks for your Mac password once), then git, Node, Python and
#      the OCR tools (ocrmypdf, Tesseract, Ghostscript);
#   4. downloads the wiki engine into ~/Wiki/<name> and sets up its local history (git,
#      on this Mac only);
#   5. with Claude, the Anthropic sign-in (stored in the macOS Keychain); with the model
#      on this Mac, its download and a first start;
#   6. with Claude, Jev, the review judge: an optional TypeSafe API key (Keychain too),
#      which turns Auto-review on;
#   7. starts two background agents, the runner (processes what you upload) and the
#      viewer (the wiki site and its Upload page at http://127.0.0.1:8765, visible only
#      to this Mac), and puts Open Wiki on your Desktop;
#   8. processes a welcome note as a test and opens the wiki.
#
# Nothing is published. The only accounts involved are Anthropic's and, if you give a key,
# TypeSafe's; none at all with the local model. Logs (no secrets):
# ~/Library/Logs/wiki-starter/install.log
#
# Answers can be given as environment variables instead of prompts:
#   WIKI_TITLE, WIKI_COMPANY, WIKI_ENGINE (claude|local), WIKI_AUTH
#   (subscription|apikey|skip), WIKI_API_KEY, WIKI_OAUTH_TOKEN, WIKI_TYPESAFE_KEY (Jev's
#   key), WIKI_JEV=skip (no Jev key now), WIKI_SKIP_SMOKE_TEST=1

set -u

ENGINE_REPO="${WIKI_ENGINE_REPO:-arkceo/wiki-starter}"
ENGINE_URL="${WIKI_ENGINE_URL:-https://codeload.github.com/$ENGINE_REPO/tar.gz/refs/heads/main}"
WIKI_ROOT="${WIKI_ROOT:-$HOME/Wiki}"
PORT="${WIKI_PORT:-8765}"
KEYCHAIN_SERVICE="wiki-starter"
LOG_DIR="$HOME/Library/Logs/wiki-starter"
LOG_FILE="$LOG_DIR/install.log"
AGENT_DIR="$HOME/Library/LaunchAgents"
AGENT_PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# The local model ("This Mac only"), picked by a side-by-side test on sample documents.
# The hash is Hugging Face's own sha256 for this exact file; change all four together.
LOCAL_MODEL_REPO="${WIKI_LOCAL_MODEL_REPO:-bartowski/Qwen_Qwen3.5-9B-GGUF}"
LOCAL_MODEL_FILE="${WIKI_LOCAL_MODEL_FILE:-Qwen_Qwen3.5-9B-Q4_K_M.gguf}"
LOCAL_MODEL_SHA256="${WIKI_LOCAL_MODEL_SHA256:-d784ce9eda1a5a7b51e8f705a9e6310844bf4f173654d115823c775fdea56d43}"
LOCAL_MODEL_GB="6.2"
MODEL_DIR="$HOME/Library/Application Support/wiki-starter/models"
ENGINE_CHOICE=""

mkdir -p "$LOG_DIR"

# ------------------------------------------------------------------ output -----
bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; log "ok: $*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; log "warn: $*"; }
die()  { printf '\n  \033[31m✗ %s\033[0m\n    Details: %s\n    Run the same command again to retry; finished steps are skipped.\n' "$*" "$LOG_FILE"; log "FAILED: $*"; exit 1; }
log()  { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"; }
step() { printf '\n'; bold "$*"; log "== $*"; }

have_tty() { { : < /dev/tty; } 2>/dev/null; }

# The terminal device itself (/dev/ttys003), for a program that needs the terminal as
# its input. macOS's kqueue rejects /dev/tty, and Claude Code's runtime reads its input
# through kqueue: given /dev/tty, `claude setup-token` crashed with "EINVAL: invalid
# argument, kqueue". Falls back to /dev/tty when the device cannot be found.
terminal_device() {
  local t
  t=$(ps -o tty= -p $$ 2>/dev/null | tr -d ' ')
  case "$t" in ""|"?"*) t="" ;; /dev/*) ;; *) t="/dev/$t" ;; esac
  if [ -n "$t" ] && [ -c "$t" ] && [ -r "$t" ] && [ -w "$t" ]; then printf '%s' "$t"; else printf '/dev/tty'; fi
}

ask() { # VAR "question" "default"
  local current answer
  eval "current=\${$1:-}"
  if [ -n "$current" ]; then return 0; fi
  if have_tty; then
    if [ -n "$3" ]; then printf '  %s [%s]: ' "$2" "$3" > /dev/tty; else printf '  %s: ' "$2" > /dev/tty; fi
    IFS= read -r answer < /dev/tty || answer=""
  else
    answer=""
  fi
  [ -n "$answer" ] || answer="$3"
  eval "$1=\$answer"
}

# A secret typed or pasted at the terminal, with every space and line break removed.
# A token printed in a narrow window runs over two lines and its paste arrives as two:
# whatever else arrives within a second of the first line is part of it.
read_secret() { # "question" -> stdout
  local answer more
  printf '  %s: ' "$1" > /dev/tty
  IFS= read -rs answer < /dev/tty || answer=""
  while IFS= read -rs -t 1 more < /dev/tty; do answer="$answer$more"; done
  printf '\n' > /dev/tty
  printf '%s' "$answer" | tr -d '[:space:]'
}

ask_secret() { # VAR "question"
  local current answer
  eval "current=\${$1:-}"
  if [ -n "$current" ]; then return 0; fi
  have_tty || { eval "$1=''"; return 0; }
  answer=$(read_secret "$2")
  eval "$1=\$answer"
}

slugify() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -e 's/[^a-z0-9]\{1,\}/-/g' -e 's/^-//' -e 's/-$//' | cut -c1-40; }

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

brew_bin() {
  if command -v brew >/dev/null 2>&1; then command -v brew
  elif [ -x /opt/homebrew/bin/brew ]; then echo /opt/homebrew/bin/brew
  elif [ -x /usr/local/bin/brew ]; then echo /usr/local/bin/brew
  fi
}

SUDO_KEEPALIVE_PID=""
stop_sudo_keepalive() { [ -n "$SUDO_KEEPALIVE_PID" ] && kill "$SUDO_KEEPALIVE_PID" 2>/dev/null; SUDO_KEEPALIVE_PID=""; }
trap stop_sudo_keepalive EXIT

# --------------------------------------------------------------- 1 preflight ---
preflight() {
  step "1/8  Checking this Mac"
  if [ "$(uname -s)" != "Darwin" ] && [ -z "${WIKI_ALLOW_NON_MAC:-}" ]; then
    die "This installer is for macOS."
  fi
  if [ "$(id -u)" = "0" ]; then die "Run this as your normal user, not with sudo."; fi
  local ver major
  ver=$(sw_vers -productVersion 2>/dev/null || echo "0")
  major=${ver%%.*}
  if [ "${major:-0}" -lt 13 ] 2>/dev/null; then
    die "macOS 13 (Ventura) or newer is needed; this Mac has $ver."
  fi
  ok "macOS $ver on $(uname -m)"
  if ! curl -fsS -o /dev/null --max-time 15 https://github.com 2>/dev/null; then
    die "No internet connection (could not reach github.com)."
  fi
  ok "online"
}

# ------------------------------------------------------------- 2 questions -----
WIKI_DIR=""
SLUG=""
questions() {
  step "2/8  Your wiki"
  local existing
  existing=$(ls -d "$WIKI_ROOT"/*/.wiki-engine 2>/dev/null | head -1)
  if [ -n "$existing" ] && [ -z "${WIKI_TITLE:-}" ]; then
    WIKI_DIR=$(dirname "$existing")
    SLUG=$(basename "$WIKI_DIR")
    ok "found the existing wiki at $WIKI_DIR (repairing and resuming)"
    return 0
  fi
  ask WIKI_COMPANY "Company name" ""
  local default_title="Wiki"
  [ -n "$WIKI_COMPANY" ] && default_title="$WIKI_COMPANY Wiki"
  ask WIKI_TITLE "Wiki title" "$default_title"
  SLUG=$(slugify "$WIKI_TITLE")
  [ -n "$SLUG" ] || SLUG="wiki"
  WIKI_DIR="$WIKI_ROOT/$SLUG"
  ok "the wiki will live in $WIKI_DIR"
}

choose_engine() {
  local current="" choice="${WIKI_ENGINE:-}" mem_gb
  [ -f "$WIKI_DIR/wiki.config.json" ] \
    && current=$(sed -n 's/.*"engine": *"\([a-z]*\)".*/\1/p' "$WIKI_DIR/wiki.config.json" | head -1)
  case "$choice" in claude) choice=1 ;; local) choice=2 ;; esac
  info "Who should read your documents?"
  info "  1) Claude (recommended): pages written and cross-linked like a careful assistant"
  info "     would, in the most natural words, and you can ask it about your wiki. Needs an"
  info "     Anthropic sign-in; the text Claude reads is sent to Anthropic."
  info "  2) This Mac only: a model runs on this Mac. Nothing leaves it and no account is"
  info "     needed. It builds the same kinds of pages and links, and checks every figure"
  info "     against its document, but takes a few minutes a document and writes plainer"
  info "     overviews. A one-off download of about $LOCAL_MODEL_GB GB."
  ask choice "Choose 1 or 2" "$([ "$current" = local ] && echo 2 || echo 1)"
  case "$choice" in
    1) ENGINE_CHOICE=claude ;;
    2) ENGINE_CHOICE=local ;;
    *) die "Please choose 1 or 2." ;;
  esac
  if [ "$ENGINE_CHOICE" = local ]; then
    mem_gb=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))
    if [ "$mem_gb" -gt 0 ] && [ "$mem_gb" -lt 16 ]; then
      warn "this Mac has $mem_gb GB of memory; the local model needs 16 GB to run well"
    fi
    [ "$(uname -m)" = arm64 ] || warn "on an Intel Mac the local model is very slow; Claude is the better choice"
  fi
  ok "documents will be read by $([ "$ENGINE_CHOICE" = local ] && echo "a model on this Mac" || echo Claude)"
}

# ------------------------------------------------------------------ 3 tools ----
install_tools() {
  step "3/8  Installing tools (a few minutes the first time)"
  local brew
  brew=$(brew_bin)
  if [ -z "$brew" ]; then
    if ! id -Gn | tr ' ' '\n' | grep -qx admin; then
      die "Homebrew needs an administrator account. Log in as an admin user, or ask one to run this."
    fi
    info "Homebrew is the standard way to install developer tools on a Mac."
    info "macOS will ask for your Mac password once."
    sudo -v < /dev/tty || die "Could not get administrator permission."
    ( while true; do sudo -n true; sleep 50; kill -0 "$$" 2>/dev/null || exit; done ) 2>/dev/null &
    SUDO_KEEPALIVE_PID=$!
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" >> "$LOG_FILE" 2>&1 \
      || die "Homebrew did not install."
    stop_sudo_keepalive
    brew=$(brew_bin)
    [ -n "$brew" ] || die "Homebrew installed but could not be found."
  fi
  eval "$("$brew" shellenv)"
  if ! grep -qs 'brew shellenv' "$HOME/.zprofile"; then
    printf '\neval "$(%s shellenv)"\n' "$brew" >> "$HOME/.zprofile"
  fi
  ok "Homebrew"

  local pkg missing="" pkgs="git node python@3.12 ocrmypdf"
  [ "$ENGINE_CHOICE" = local ] && pkgs="$pkgs llama.cpp"
  for pkg in $pkgs; do
    "$brew" list --formula "$pkg" >/dev/null 2>&1 || missing="$missing $pkg"
  done
  if [ -n "$missing" ]; then
    info "installing:$missing"
    # shellcheck disable=SC2086
    HOMEBREW_NO_AUTO_UPDATE=1 "$brew" install $missing >> "$LOG_FILE" 2>&1 || die "Could not install:$missing"
  fi
  ok "git, Node, Python 3.12, OCR tools$([ "$ENGINE_CHOICE" = local ] && echo ", llama.cpp")"

  # The local model needs no Claude Code (and no account).
  [ "$ENGINE_CHOICE" = local ] && return 0
  if ! command -v claude >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/claude" ]; then
    info "installing Claude Code"
    curl -fsSL https://claude.ai/install.sh | bash >> "$LOG_FILE" 2>&1 || die "Claude Code did not install."
  fi
  export PATH="$HOME/.local/bin:$PATH"
  command -v claude >/dev/null 2>&1 || die "Claude Code installed but could not be found."
  ok "Claude Code"
}

# ----------------------------------------------------------------- 4 engine ----
python_bin() {
  local p
  for p in "$(brew_bin 2>/dev/null | xargs dirname 2>/dev/null)/python3.12" /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3.12 python3; do
    if command -v "$p" >/dev/null 2>&1 || [ -x "$p" ]; then echo "$p"; return 0; fi
  done
  return 1
}

install_engine() {
  step "4/8  Setting up the wiki"
  if [ -f "$WIKI_DIR/.wiki-engine/VERSION" ]; then
    ok "engine already in place ($(cat "$WIKI_DIR/.wiki-engine/VERSION")); use Update Wiki Engine to update it"
  else
    if [ -e "$WIKI_DIR" ] && [ -n "$(ls -A "$WIKI_DIR" 2>/dev/null)" ]; then
      die "$WIKI_DIR already exists and is not a wiki. Choose another title or move that folder."
    fi
    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/wiki-starter.XXXXXX")
    if [ -n "${WIKI_ENGINE_DIR:-}" ]; then
      cp -R "$WIKI_ENGINE_DIR"/. "$tmp"/ || die "Could not copy the engine from $WIKI_ENGINE_DIR."
    else
      curl -fsSL "$ENGINE_URL" | tar -xzf - -C "$tmp" --strip-components 1 \
        || die "Could not download the wiki engine."
    fi
    [ -f "$tmp/.wiki-engine/VERSION" ] || die "The download does not look like the wiki engine."
    mkdir -p "$WIKI_DIR"
    cp -R "$tmp"/. "$WIKI_DIR"/ || die "Could not copy the engine into $WIKI_DIR."
    rm -rf "$tmp"
    ok "engine $(cat "$WIKI_DIR/.wiki-engine/VERSION") copied into $WIKI_DIR"
  fi
  cd "$WIKI_DIR" || die "Cannot open $WIKI_DIR."
  chmod +x scripts/*.sh ./*.command install.sh 2>/dev/null

  local py
  py=$(python_bin) || die "Python 3.12 not found."
  mkdir -p .wiki-engine/state
  if [ ! -f .wiki-engine/state/config.done ]; then
    WIKI_TITLE_V="${WIKI_TITLE:-Wiki}" WIKI_COMPANY_V="${WIKI_COMPANY:-}" WIKI_PORT_V="$PORT" "$py" - <<'PYEOF' || die "Could not write wiki.config.json."
import json, os, re
cfg = {}
try:
    cfg = json.load(open("wiki.config.json"))
except Exception:
    pass
cfg["title"] = os.environ["WIKI_TITLE_V"]
cfg["company"] = os.environ["WIKI_COMPANY_V"]
cfg["port"] = int(os.environ["WIKI_PORT_V"])
# Performance mode and reading speed come from the shipped wiki.config.json (Maximum,
# fastest); scripts/wiki_settings.py holds the figures.
json.dump(cfg, open("wiki.config.json", "w"), indent=2)
open("wiki.config.json", "a").write("\n")
company = os.environ["WIKI_COMPANY_V"]
if company:
    p = "HOUSE-RULES.md"
    s = open(p).read()
    s = re.sub(r"^- Company name:\s*$", "- Company name: " + company, s, count=1, flags=re.M)
    open(p, "w").write(s)
PYEOF
    : > .wiki-engine/state/config.done
  fi
  ok "settings saved in wiki.config.json"

  if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import markitdown, pypdf" 2>/dev/null; then
    info "installing the document converter (MarkItDown)"
    rm -rf .venv
    "$py" -m venv .venv >> "$LOG_FILE" 2>&1 \
      && .venv/bin/python -m pip install --quiet --upgrade pip >> "$LOG_FILE" 2>&1 \
      && .venv/bin/python -m pip install --quiet -r scripts/requirements.txt >> "$LOG_FILE" 2>&1 \
      || { rm -rf .venv; die "Could not install the document converter."; }
  fi
  ok "document converter"

  if [ ! -d node_modules ] || [ package-lock.json -nt node_modules ]; then
    info "installing the site builder"
    npm ci --no-audit --no-fund >> "$LOG_FILE" 2>&1 || die "Could not install the site builder."
    touch node_modules
  fi
  ok "site builder"

  if [ ! -d .git ]; then
    git init -q -b main >> "$LOG_FILE" 2>&1 || git init -q >> "$LOG_FILE" 2>&1 || die "git init failed."
    git add -A -- . >> "$LOG_FILE" 2>&1
    git -c user.name="Wiki installer" -c user.email="installer@localhost" \
      commit -q -m "install: wiki-starter $(cat .wiki-engine/VERSION)" >> "$LOG_FILE" 2>&1 \
      || die "First commit failed."
  fi
  ok "local history (git, never pushed anywhere)"

  # Build the site now, before the agents start, so the runner's at-login run finds
  # nothing to do instead of racing this build for the lock.
  if [ ! -f public/index.html ]; then
    info "building the site"
    bash scripts/wiki_runner.sh --rebuild >> "$LOG_FILE" 2>&1 || warn "the first site build failed; see $LOG_DIR/runner.log"
  fi
  [ -f public/index.html ] && ok "site built"
}

# The engine is recorded only once step 5 has succeeded: a background run that started
# while the model was still downloading would otherwise find no model and fail.
set_engine() { # claude|local
  WIKI_ENGINE_V="$1" .venv/bin/python - <<'PYEOF' || die "Could not write wiki.config.json."
import json, os
cfg = json.load(open("wiki.config.json"))
cfg["engine"] = os.environ["WIKI_ENGINE_V"]
json.dump(cfg, open("wiki.config.json", "w"), indent=2)
open("wiki.config.json", "a").write("\n")
PYEOF
}

# ------------------------------------------------------------ 5 local model ----
sha256_of() { shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1; }

local_model_setup() {
  step "5/8  The model on this Mac"
  local path="$MODEL_DIR/$LOCAL_MODEL_FILE"
  mkdir -p "$MODEL_DIR"
  if [ -f "$path" ] && [ "$(sha256_of "$path")" = "$LOCAL_MODEL_SHA256" ]; then
    ok "model already downloaded ($LOCAL_MODEL_FILE)"
  else
    info "downloading $LOCAL_MODEL_FILE (about $LOCAL_MODEL_GB GB) from Hugging Face."
    info "This takes a while; if it stops, run the installer again and it resumes."
    curl -fL -C - --retry 5 --retry-delay 5 --progress-bar -o "$path.part" \
      "https://huggingface.co/$LOCAL_MODEL_REPO/resolve/main/$LOCAL_MODEL_FILE" \
      || die "The model download stopped. Run the installer again to resume it."
    info "checking the download"
    if [ "$(sha256_of "$path.part")" != "$LOCAL_MODEL_SHA256" ]; then
      rm -f "$path.part"
      die "The downloaded model does not match its published checksum. Run the installer again."
    fi
    mv "$path.part" "$path"
    ok "model downloaded and verified"
  fi
  WIKI_MODEL_V="$path" .venv/bin/python - <<'PYEOF' || die "Could not write wiki.config.json."
import json, os
cfg = json.load(open("wiki.config.json"))
lm = cfg.get("localModel") or {}
lm["file"] = os.environ["WIKI_MODEL_V"]
lm.setdefault("ctx", 32768)
cfg["localModel"] = lm
json.dump(cfg, open("wiki.config.json", "w"), indent=2)
open("wiki.config.json", "a").write("\n")
PYEOF
  info "starting the model once to check it works (up to a minute)"
  .venv/bin/python scripts/local_engine.py check >> "$LOG_FILE" 2>&1 \
    || die "The local model did not start. Details are in $LOG_FILE and $LOG_DIR/runner.log."
  ok "the model answers, on this Mac only"
}

# ------------------------------------------------------------ 5 anthropic ------
keychain_has() { security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$1" >/dev/null 2>&1; }
keychain_put() { # account secret [label]
  security add-generic-password -U -s "$KEYCHAIN_SERVICE" -a "$1" -l "${3:-Wiki ($1)}" -w "$2" >/dev/null 2>&1
}
keychain_del() { security delete-generic-password -s "$KEYCHAIN_SERVICE" -a "$1" >/dev/null 2>&1; }

check_api_key() { # key -> http status of a free models listing
  printf 'x-api-key: %s\nanthropic-version: 2023-06-01\n' "$1" \
    | curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -H @- https://api.anthropic.com/v1/models 2>/dev/null
}

check_oauth_token() { # token -> 0 if claude answers with it
  local out
  out=$(cd "${TMPDIR:-/tmp}" && env -u ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN="$1" \
    claude -p "Reply with exactly: OK" --max-turns 1 2>/dev/null < /dev/null)
  printf '%s' "$out" | grep -q "OK"
}

anthropic_signin() {
  step "5/8  Anthropic sign-in"
  if keychain_has claude-oauth-token || keychain_has anthropic-api-key; then
    local replace="${WIKI_REPLACE_AUTH:-}"
    [ -n "${WIKI_AUTH:-}" ] && replace="y"
    ask replace "A sign-in is already stored. Replace it? (y/N)" "n"
    case "$replace" in y|Y|yes|YES) ;; *) ok "using the stored sign-in"; return 0 ;; esac
  fi
  info "Claude needs an Anthropic account to read your documents. Choose one:"
  info "  1) Claude subscription (Pro or Max): sign in through the browser"
  info "  2) Anthropic API key (pay as you go, from console.anthropic.com)"
  local choice="${WIKI_AUTH:-}"
  case "$choice" in subscription) choice=1 ;; apikey) choice=2 ;; skip) choice=s ;; esac
  ask choice "Choose 1 or 2" "1"
  case "$choice" in
    1)
      local token="${WIKI_OAUTH_TOKEN:-}"
      if [ -z "$token" ]; then
        info "A browser window opens. Sign in, approve, then come back here."
        info "Claude Code prints a long token starting with sk-ant-oat. Copy all of it:"
        info "in a narrow window it runs over two lines; copy both."
        if have_tty; then
          local term; term=$(terminal_device)
          if ! claude setup-token < "$term" > "$term" 2>&1; then
            log "claude setup-token exited with an error (terminal $term)"
            warn "The sign-in did not finish here. Open a new Terminal window (Cmd+N),"
            info "  run: $(command -v claude) setup-token"
            info "  then copy the token it prints and paste it below."
          fi
        fi
        ask_secret token "Paste the token (it stays hidden)"
      fi
      [ -n "$token" ] || die "No token entered."
      # A token cut off at a line break gets its second half; a new whole token replaces it.
      local tries=1 more
      until check_oauth_token "$token"; do
        if [ "$tries" -ge 3 ] || [ -n "${WIKI_OAUTH_TOKEN:-}" ] || ! have_tty; then
          die "That token did not work. Run the installer again and paste the whole token."
        fi
        tries=$((tries + 1))
        warn "That token did not work. If it was cut off, paste the rest of it;"
        more=$(read_secret "otherwise paste the whole token again")
        case "$more" in sk-ant-*) token="$more" ;; *) token="$token$more" ;; esac
      done
      keychain_del anthropic-api-key
      keychain_put claude-oauth-token "$token" || die "Could not save the token in the Keychain."
      ok "subscription sign-in stored in the Keychain"
      ;;
    2)
      local key="${WIKI_API_KEY:-}"
      if [ -z "$key" ]; then
        info "Create a key at https://console.anthropic.com/settings/keys"
        info "and set a monthly spend limit there."
        have_tty && open "https://console.anthropic.com/settings/keys" 2>/dev/null
        ask_secret key "Paste the API key (it stays hidden)"
      fi
      [ -n "$key" ] || die "No key entered."
      [ "$(check_api_key "$key")" = "200" ] || die "Anthropic did not accept that key."
      keychain_del claude-oauth-token
      keychain_put anthropic-api-key "$key" || die "Could not save the key in the Keychain."
      ok "API key stored in the Keychain"
      ;;
    s) warn "skipped: the wiki will not process anything until you run the installer again" ;;
    *) die "Please choose 1 or 2." ;;
  esac
}

# ------------------------------------------------------------------ 6 jev ------
# Jev, TypeSafe's decision model, scores the answers to the review questions
# (scripts/wiki_jev.py). It is optional: without a key the wiki works the same and the
# owner answers every question, so nothing in this step stops the installer. Settings on
# the Upload page checks and stores a key the same way, later.
JEV_ACCOUNT="typesafe-api-key"
JEV_LABEL="Wiki (TypeSafe Jev API key)"   # the label wiki_jev.set_key gives it
JEV_LATER="add a TypeSafe key later in Settings on the Upload page"

looks_like_key() { # key -> 0 for 1 to 512 printable ASCII characters, as Settings checks
  [ -n "$1" ] && [ "${#1}" -le 512 ] && ! printf '%s' "$1" | LC_ALL=C grep -q '[^[:print:]]'
}

# Jev's own check of a key. The key goes in on stdin, never as an argument (ps would show
# it) or in the environment. Prints TypeSafe's one-line answer; returns 0 when Jev
# answered, 1 when TypeSafe refused the key, 2 when it could not be checked now.
check_jev_key() { # key -> message
  local out rc
  out=$(printf '%s' "$1" | .venv/bin/python scripts/wiki_jev.py test-key 2>/dev/null)
  rc=$?
  log "TypeSafe key check: exit $rc"
  out=$(printf '%s\n' "$out" | tail -n 1)
  case "$rc" in 0|1) ;; *) rc=2 ;; esac
  if [ -z "$out" ]; then out="the check gave no answer"; rc=2; fi
  printf '%s' "$out"
  return "$rc"
}

# Auto-review on, unless the owner chose the review mode while a key was stored (a run
# again keeps it). Without a key, Auto-review could not be chosen, so a "manual" saved by
# Settings then (it saves the mode with every change) is not a choice. Prints "set" or
# "kept". $1: 1 if a key was stored before this step.
jev_auto_review() {
  JEV_HAD_KEY="$1" .venv/bin/python - 2>> "$LOG_FILE" <<'PYEOF'
import json, os
cfg = json.load(open("wiki.config.json"))
review = cfg.get("review") if isinstance(cfg.get("review"), dict) else None
if review is not None and (os.environ.get("JEV_HAD_KEY") == "1" or review.get("mode") == "auto"):
    print("kept")
else:
    cfg["review"] = dict(review or {}, mode="auto", autoConfidence=0.7)
    json.dump(cfg, open("wiki.config.json", "w"), indent=2)
    open("wiki.config.json", "a").write("\n")
    print("set")
PYEOF
}

review_mode() { # auto|manual, read the way the engine reads it
  .venv/bin/python -c 'import sys; sys.path.insert(0, "scripts"); import wiki_jev
print(wiki_jev.review_settings()["mode"])' 2>/dev/null
}

jev_not_stored() { # ok|warn: how the step ends when no new key was stored
  if keychain_has "$JEV_ACCOUNT"; then "$1" "kept the stored TypeSafe key"
  else "$1" "skipped: Jev stays off; $JEV_LATER"; fi
}

jev_setup() {
  step "6/8  Jev, the review judge (TypeSafe)"
  if [ "$ENGINE_CHOICE" = local ]; then
    ok "Jev is off: with the model on this Mac, nothing leaves it"
    return 0
  fi
  local had_key=0
  if keychain_has "$JEV_ACCOUNT"; then
    had_key=1
    local replace=""
    [ -n "${WIKI_TYPESAFE_KEY:-}" ] && replace="y"
    [ "${WIKI_JEV:-}" = skip ] && replace="n"
    ask replace "A TypeSafe key is already stored. Replace it? (y/N)" "n"
    case "$replace" in y|Y|yes|YES) ;; *) ok "using the stored TypeSafe key"; return 0 ;; esac
  elif [ "${WIKI_JEV:-}" = skip ]; then
    ok "skipped: $JEV_LATER"
    return 0
  fi
  info "Jev, TypeSafe's decision model, scores the answers to each review question. With"
  info "Auto-review on, it answers the questions the documents themselves settle. Only a"
  info "question, its options and the excerpts it is about go to TypeSafe, only with a key."
  local key tries=0 msg rc
  key=$(printf '%s' "${WIKI_TYPESAFE_KEY:-}" | tr -d '[:space:]')
  if [ -z "$key" ]; then
    info "Get a key at https://console.typesafe.ai, or press Enter to skip: you can"
    info "add one later in Settings on the Upload page."
    have_tty && open "https://console.typesafe.ai" 2>/dev/null
    ask_secret key "Paste the TypeSafe key (it stays hidden; Enter skips)"
  fi
  while :; do
    if [ -z "$key" ]; then jev_not_stored ok; return 0; fi
    tries=$((tries + 1))
    if looks_like_key "$key"; then
      msg=$(check_jev_key "$key"); rc=$?
    else
      msg="that does not look like an API key"; rc=1
    fi
    if [ "$rc" = 0 ]; then ok "$msg"; break; fi
    if [ "$rc" = 2 ]; then warn "the key could not be checked just now ($msg); storing it anyway"; break; fi
    warn "$msg"
    if [ "$tries" -ge 3 ] || [ -n "${WIKI_TYPESAFE_KEY:-}" ] || ! have_tty; then
      jev_not_stored warn
      return 0
    fi
    key=""
    ask_secret key "Paste the key again (it stays hidden; Enter skips)"
  done
  if ! keychain_put "$JEV_ACCOUNT" "$key" "$JEV_LABEL"; then
    warn "the key could not be saved in the Keychain; $JEV_LATER"
    return 0
  fi
  ok "TypeSafe key stored in the Keychain"
  case "$(jev_auto_review "$had_key")" in
    set)  ok "Auto-review is on: Jev answers the questions the documents settle" ;;
    kept) ok "review mode left as you set it ($(review_mode))" ;;
    *)    warn "Auto-review could not be turned on; turn it on in the Upload page's Review tab" ;;
  esac
}

jev_summary() { # the Done. line about Jev (Claude only)
  if ! keychain_has "$JEV_ACCOUNT"; then
    info "Review:        Jev is off; $JEV_LATER"
  elif [ "$(review_mode)" = auto ]; then
    info "Review:        Auto-review is on: Jev answers what the documents settle"
  else
    info "Review:        Jev scores the answers; Auto-review is off (Review tab)"
  fi
}

# --------------------------------------------------------------- 7 agents ------
write_plist() { # path label program-args-xml extra-xml
  cat > "$1" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$2</string>
  <key>ProgramArguments</key>
  <array>
$3
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$WIKI_DIR")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(xml_escape "$AGENT_PATH")</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
$4
  <key>StandardOutPath</key><string>$(xml_escape "$LOG_DIR/$2.out.log")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOG_DIR/$2.err.log")</string>
</dict>
</plist>
EOF
}

load_agent() { # plist label
  launchctl bootout "gui/$(id -u)/$2" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$1" >> "$LOG_FILE" 2>&1 || die "Could not start the background agent $2."
}

install_agents() {
  step "7/8  Starting the background agents"
  mkdir -p "$AGENT_DIR"
  local runner="local.wiki-starter.$SLUG.runner" viewer="local.wiki-starter.$SLUG.viewer"
  local d; d=$(xml_escape "$WIKI_DIR")

  write_plist "$AGENT_DIR/$runner.plist" "$runner" \
"    <string>/bin/bash</string>
    <string>$d/scripts/wiki_runner.sh</string>" \
"  <key>WatchPaths</key>
  <array>
    <string>$d/raw/inbox</string>
    <string>$d/raw/_intake</string>
  </array>
  <key>StartInterval</key><integer>900</integer>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Standard</string>"
  load_agent "$AGENT_DIR/$runner.plist" "$runner"
  ok "runner: processes everything you upload"

  write_plist "$AGENT_DIR/$viewer.plist" "$viewer" \
"    <string>$d/.venv/bin/python</string>
    <string>$d/scripts/wiki_server.py</string>" \
"  <key>KeepAlive</key><true/>"
  load_agent "$AGENT_DIR/$viewer.plist" "$viewer"
  ok "viewer: http://127.0.0.1:$PORT (this Mac only)"

  # Desktop: Open Wiki only. Documents and notes go in through the Upload page.
  local desk="$HOME/Desktop" opener="Open Wiki.webloc"
  mkdir -p "$desk"
  remove_old_drop_links "$desk"
  if [ -e "$desk/$opener" ] && ! grep -q "127.0.0.1:$PORT/" "$desk/$opener" 2>/dev/null; then
    opener="Open $WIKI_TITLE_SHOWN.webloc"  # another wiki on this Mac already has the name
  fi
  cat > "$desk/$opener" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>URL</key><string>http://127.0.0.1:$PORT/</string></dict></plist>
EOF
  ok "Desktop: ${opener%.webloc} (add documents with Upload, top right of the wiki)"
}

# Earlier versions put two folder shortcuts (Wiki Inbox, Wiki Intake) on the Desktop.
# Remove a Desktop item only when it is a link into this wiki's queue folders.
remove_old_drop_links() { # desktop-dir
  local item target
  for item in "$1"/*; do
    [ -L "$item" ] || continue
    target=$(readlink "$item")
    case "$target" in
      "$WIKI_DIR/raw/inbox"|"$WIKI_DIR/raw/_intake") rm -f "$item" && log "removed old Desktop link $item" ;;
    esac
  done
}

# ------------------------------------------------------------ 8 first run ------
first_run() {
  step "8/8  First test"
  if [ -n "${WIKI_SKIP_SMOKE_TEST:-}" ] || [ -n "$(ls archive/inbox/*.md 2>/dev/null)" ]; then
    ok "test skipped"
  elif [ "$ENGINE_CHOICE" != local ] && ! keychain_has claude-oauth-token && ! keychain_has anthropic-api-key; then
    warn "test skipped: no Anthropic sign-in stored"
  else
    local today packet waited=0
    today=$(date +%F)
    packet="raw/inbox/update-$today-general.md"
    sed -e "s/WELCOME_DATE/$today/g" -e "s/WELCOME_PORT/$PORT/g" \
        -e "s/WELCOME_COMPANY/$(printf '%s' "${WIKI_COMPANY:-this business}" | sed 's/[&/\]/\\&/g')/g" \
        engine/welcome-packet.md > "$packet"
    info "queued a welcome note; waiting for the runner (up to 5 minutes)"
    while [ -f "$packet" ] && [ "$waited" -lt 300 ]; do sleep 10; waited=$((waited + 10)); printf '.'; done
    printf '\n'
    if [ ! -f "$packet" ]; then
      # Give the rebuild a moment to finish after the packet was archived.
      sleep 20
      ok "the runner processed the welcome note"
    else
      warn "the runner has not finished yet. It keeps going in the background; see $LOG_DIR/runner.log"
    fi
  fi
  open "http://127.0.0.1:$PORT/" 2>/dev/null || true
}

# ------------------------------------------------------------------- main ------
# Everything runs inside main(), called on the last line, so bash has read the whole
# script before any step starts. With `curl ... | bash`, bash reads this file from the
# same pipe every command it starts inherits as stdin; a command that reads stdin
# (Homebrew does while installing) would otherwise swallow the rest of the script and
# the installer would stop silently after step 3. Prompts read /dev/tty directly, so
# stdin is pointed at /dev/null for good measure. A test sets WIKI_INSTALL_NO_MAIN to
# source this file and run one step on its own.
main() {
  exec < /dev/null
  printf '\n'
  bold "Wiki installer"
  info "Everything stays on this Mac. Log: $LOG_FILE"
  log "installer started (engine $ENGINE_REPO)"

  preflight
  questions
  WIKI_TITLE_SHOWN="${WIKI_TITLE:-$SLUG}"
  choose_engine
  install_tools
  install_engine
  if [ "$ENGINE_CHOICE" = local ]; then local_model_setup; else anthropic_signin; fi
  jev_setup
  set_engine "$ENGINE_CHOICE"
  install_agents
  first_run

  printf '\n'
  bold "Done."
  info "Wiki folder:   $WIKI_DIR"
  info "Read it:       http://127.0.0.1:$PORT  (Open Wiki on the Desktop)"
  info "Add documents: click Upload, top right of the wiki, and drop files on it"
  info "Add notes:     upload Update Packets the same way"
  info "Your rules:    edit HOUSE-RULES.md in the wiki folder"
  if [ "$ENGINE_CHOICE" = local ]; then
    info "Reading:       a model on this Mac; nothing leaves it"
  else
    info "Ask questions: open the wiki folder in Claude (desktop app or 'claude' in Terminal)"
    jev_summary
  fi
  info "Turn on Time Machine: your documents are not kept anywhere else."
  printf '\n'
}

[ -n "${WIKI_INSTALL_NO_MAIN:-}" ] || main "$@"
