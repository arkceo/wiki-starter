#!/bin/bash
# Update Wiki Engine — download the newest wiki engine and replace engine files only.
#
# Double-click it, or run:  bash "Update Wiki Engine.command" [--force]
#
# What it touches: the files listed in .wiki-engine/files.txt (scripts, the site code,
# prompts, CLAUDE.md, these buttons). What it never touches: wiki/, raw/, archive/,
# generated/<your projects>, index.md, log.md, HOUSE-RULES.md, wiki.config.json, and the
# folder's history.
#
# An engine file you edited yourself is copied to .wiki-engine/backup/<time>/ before it
# is replaced, so nothing is lost. The update is one local git commit; `git revert` undoes
# it. No account is needed: the engine is downloaded from a public address.

set -u
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1
SOURCE="$(cat .wiki-engine/source 2>/dev/null || echo "arkceo/wiki-starter")"
URL="${WIKI_ENGINE_URL:-https://codeload.github.com/$SOURCE/tar.gz/refs/heads/main}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP=".wiki-engine/backup/$STAMP"

say()  { printf '%s\n' "$*"; }
fail() { say ""; say "✗ $*"; finish 1; }
finish() {
  [ -n "${TMP:-}" ] && rm -rf "$TMP"
  if [ -t 0 ] && [ -z "${WIKI_NO_PAUSE:-}" ]; then read -r -p "Press Return to close. " _; fi
  exit "$1"
}

hash_of() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else sha256sum "$1" | cut -d' ' -f1; fi
}
manifest_hash() { # manifest path -> hash or empty
  awk -v p="$2" 'BEGIN{FS="  "} { h=$1; sub(/^[^ ]+  /, ""); if ($0 == p) { print h; exit } }' "$1" 2>/dev/null
}
manifest_paths() { sed 's/^[^ ]*  //' "$1"; }

say "Wiki engine update"
[ -f .wiki-engine/files.txt ] || fail "This folder does not look like a wiki (no .wiki-engine/files.txt)."
if [ -d .wiki-engine/state/runner.lock ]; then
  fail "The wiki is processing files right now. Try again in a few minutes."
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/wiki-engine.XXXXXX")"
if [ -n "${WIKI_ENGINE_DIR:-}" ]; then
  cp -R "$WIKI_ENGINE_DIR"/. "$TMP"/ || fail "Could not read $WIKI_ENGINE_DIR."
else
  say "Downloading the newest engine…"
  curl -fsSL "$URL" | tar -xzf - -C "$TMP" --strip-components 1 || fail "Download failed. Check the internet connection."
fi
[ -f "$TMP/.wiki-engine/files.txt" ] && [ -f "$TMP/.wiki-engine/VERSION" ] || fail "The download does not look like the wiki engine."

OLD_VERSION="$(cat .wiki-engine/VERSION 2>/dev/null || echo unknown)"
NEW_VERSION="$(cat "$TMP/.wiki-engine/VERSION")"
if [ "$OLD_VERSION" = "$NEW_VERSION" ] && [ "$FORCE" = 0 ]; then
  say "Already up to date ($OLD_VERSION)."
  finish 0
fi
say "Updating $OLD_VERSION → $NEW_VERSION"

OLD_MANIFEST=".wiki-engine/files.txt"
NEW_MANIFEST="$TMP/.wiki-engine/files.txt"

protected() { # paths an engine update must never write, whatever a manifest says
  case "$1" in
    wiki/*|raw/*|archive/*|index.md|log.md|HOUSE-RULES.md|wiki.config.json|.git/*|.wiki-engine/backup/*|.wiki-engine/state/*) return 0 ;;
    generated/_templates/*) return 1 ;;
    generated/*) return 0 ;;
    /*|*../*) return 0 ;;
  esac
  return 1
}

backup() { # path
  mkdir -p "$BACKUP/$(dirname "$1")"
  cp -p "$1" "$BACKUP/$1"
  BACKED_UP=$((BACKED_UP + 1))
  say "  kept your edited copy: $BACKUP/$1"
}

ADDED=0; CHANGED=0; REMOVED=0; BACKED_UP=0
CHANGED_LIST="$TMP/.changed"; : > "$CHANGED_LIST"

# Add or replace.
while IFS= read -r p; do
  [ -n "$p" ] || continue
  if protected "$p"; then say "  skipped protected path: $p"; continue; fi
  new_h="$(manifest_hash "$NEW_MANIFEST" "$p")"
  if [ -f "$p" ]; then
    cur_h="$(hash_of "$p")"
    [ "$cur_h" = "$new_h" ] && continue
    old_h="$(manifest_hash "$OLD_MANIFEST" "$p")"
    [ "$cur_h" = "$old_h" ] || backup "$p"
    CHANGED=$((CHANGED + 1))
  else
    ADDED=$((ADDED + 1))
  fi
  mkdir -p "$(dirname "$p")"
  cp -p "$TMP/$p" "$p" || fail "Could not write $p."
  printf '%s\n' "$p" >> "$CHANGED_LIST"
done <<EOF
$(manifest_paths "$NEW_MANIFEST")
EOF

# Remove engine files the new engine no longer ships.
while IFS= read -r p; do
  [ -n "$p" ] || continue
  protected "$p" && continue
  [ -n "$(manifest_hash "$NEW_MANIFEST" "$p")" ] && continue
  [ -f "$p" ] || continue
  [ "$(hash_of "$p")" = "$(manifest_hash "$OLD_MANIFEST" "$p")" ] || backup "$p"
  rm -f "$p"
  REMOVED=$((REMOVED + 1))
  printf '%s\n' "$p" >> "$CHANGED_LIST"
done <<EOF
$(manifest_paths "$OLD_MANIFEST")
EOF

for f in files.txt VERSION CHANGELOG.md source; do
  [ -f "$TMP/.wiki-engine/$f" ] && cp -p "$TMP/.wiki-engine/$f" ".wiki-engine/$f"
done
chmod +x scripts/*.sh ./*.command install.sh 2>/dev/null

say "  $ADDED added, $CHANGED replaced, $REMOVED removed, $BACKED_UP edited file(s) backed up"

if grep -qx 'package-lock.json' "$CHANGED_LIST" || [ ! -d node_modules ]; then
  say "Updating the site builder…"
  npm ci --no-audit --no-fund > /dev/null 2>&1 || fail "npm ci failed; run this again."
fi
if grep -qx 'scripts/requirements.txt' "$CHANGED_LIST"; then
  say "Updating the document converter…"
  .venv/bin/python -m pip install --quiet -r scripts/requirements.txt || fail "pip install failed; run this again."
fi

if [ -d .git ]; then
  # Stage engine changes only; your pages and settings are never part of this commit.
  git add -A -- . ':!wiki' ':!raw' ':!archive' ':!generated' ':!index.md' ':!log.md' \
    ':!HOUSE-RULES.md' ':!wiki.config.json' > /dev/null 2>&1
  [ -d generated/_templates ] && git add -A -- generated/_templates > /dev/null 2>&1
  git -c user.name="Wiki updater" -c user.email="updater@localhost" \
    commit -q -m "engine: update $OLD_VERSION -> $NEW_VERSION" > /dev/null 2>&1 \
    && say "Recorded in the local history (undo with: git revert HEAD)."
fi

say "Rebuilding the site…"
bash scripts/wiki_runner.sh --rebuild > /dev/null 2>&1 || say "  (the site will be rebuilt on the next run)"

if command -v launchctl >/dev/null 2>&1; then
  for label in $(launchctl list 2>/dev/null | awk '{print $3}' | grep '^local\.wiki-starter\..*\.viewer$'); do
    launchctl kickstart -k "gui/$(id -u)/$label" > /dev/null 2>&1 || true
  done
fi

say ""
say "✓ Engine updated to $NEW_VERSION."
[ -f .wiki-engine/CHANGELOG.md ] && { say ""; head -20 .wiki-engine/CHANGELOG.md; }
finish 0
