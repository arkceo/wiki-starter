# Changelog

Newest first. Shown to the SME after "Update Wiki Engine".

## Upload & Logs, and a model on this Mac
- An **Upload** button at the top right of every page opens the Upload & Logs page: drop
  files or whole folders, watch each one go from uploaded to filed, and follow a live log
  of what the wiki is doing. **Process now** starts a run straight away.
- The Wiki Inbox and Wiki Intake shortcuts are no longer needed. This update removes them
  from the Desktop (macOS may ask whether Terminal may access the Desktop; if you say no,
  delete them yourself). Copying files into `raw/_intake/` or `raw/inbox/` still works.
- The home page and *How this wiki works* now describe the Upload page, if nobody has
  changed them since the install.
- New: documents can be read by a model that runs on this Mac instead of Claude. Nothing
  leaves the Mac and no account is needed. Run the install line again and choose 2 to
  switch.
- Safer originals: a document a browser could run (HTML, SVG, XML) is downloaded, never
  opened inside the wiki.

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
