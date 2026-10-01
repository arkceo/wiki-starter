# Changelog

Newest first. Shown to the SME after "Update Wiki Engine".

## Install guide link
- The README links the step-by-step install guide, from install to uninstall.

## Clearer processing
- A "Wiki is working" notice when a run starts, not only when it ends.
- Files dropped while a run is busy are processed straight after it, instead of waiting
  up to 15 minutes.
- The same file dropped into both Wiki Inbox and Wiki Intake is processed once.
- Process Now explains when a run is already in progress and how to watch it.

## Installer fix
- The one-line installer could stop silently after step 3 ("Claude Code") when Homebrew
  had tools to install. Running the same line again now finishes the remaining steps.

## First release
- Local-only wiki: drop folders on the Desktop, background runner, site at
  http://127.0.0.1:8765 served to this Mac only.
- One-line installer: Homebrew tools, Claude Code, Anthropic sign-in in the Keychain.
- Update Wiki Engine: replaces engine files only, backs up anything you edited.
