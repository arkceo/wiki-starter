# Changelog

Newest first. Shown to the SME after "Update Wiki Engine".

## README: local network permission
- The README says how to switch the "Python" local network permission off again if it
  was allowed earlier (System Settings → Privacy & Security → Local Network).

## README: updating during a large upload
- The README says when to update (when the Upload page shows Idle, since the update waits
  for a run to finish), and that "Waiting to convert" during a large upload means a later
  batch, not a problem. It also states plainly that with This Mac only, nothing leaves
  the Mac and Claude is never used.

## Large uploads in batches
- Documents are now read in batches: 8 at a time with Claude, 20 with the model on this
  Mac. Each batch is converted and read before the next, so a large upload is read in
  full and its first pages appear within minutes instead of at the end.
- A document only counts as tried if its batch actually ran. Before, a large upload could
  leave unread documents set aside in `raw/_needs-review/` as if they had failed.
- With Claude, each batch has its own spending cap (default US$5, now
  `maxSpendPerBatchUsd`; an existing `maxSpendPerRunUsd` setting still applies), and
  stopping at a cap no longer says "Claude could not run".
- With the model on this Mac, a long upload is no longer cut off after 6 hours. The model
  is stopped only if it shows no progress for 20 minutes, and the notice says so. Claude
  is never started and no Anthropic account is used.

## Live progress
- The Upload page has a live line that says what the wiki is doing at this moment: which
  file, which step (converting, reading a scan, asking the model, writing pages, filing),
  how long it has taken, and with the model on this Mac, how much of its answer it has
  written so far.
- Each file's bar now shows only real stages: upload, convert, read and write, file. A
  file that is waiting is labelled as waiting, not shown as part-done, and the overall
  percentage counts only finished files (the lighter part of the bar shows files already
  converted).
- macOS should no longer ask whether "Python" may find devices on your local network. The
  wiki never needed that: it only talks to this Mac. If you allowed it earlier, you can
  switch it off in System Settings → Privacy & Security → Local Network.

## Full guide in the README
- The README now holds the whole guide, from install to uninstall, including the
  troubleshooting table and the uninstall commands.

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
