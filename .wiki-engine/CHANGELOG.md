# Changelog

Newest first. Shown to the SME after "Update Wiki Engine".

## Upload & Logs is a one-screen dashboard
- On a laptop screen, everything fits without scrolling:
  - the state, a large "13 of 14 done" with its progress bar, and the live line;
  - counts of files in progress, waiting, done and needing attention;
  - the files and the live log side by side, each scrolling on its own.
- Files are grouped: in progress first, then needs attention, waiting and done. Click a
  count to show only those files.
- The live log has a "Problems only" switch. It keeps following new lines until you
  scroll up, and a "Latest" button brings it back.
- Drop files anywhere on the page. The status updates as soon as an upload finishes or a
  run starts or ends, instead of up to 4 seconds later.
- The page shows which reader is in use (Claude or This Mac only). Files set aside after
  failing twice are counted, with a link to the Review queue.
- In a narrow window, the files and the log share one panel with tabs.

## Subscription sign-in no longer crashes during install
- Choosing the Claude subscription sign-in could crash with "EINVAL: invalid argument,
  kqueue". The installer gave Claude Code the terminal through `/dev/tty`, which macOS
  cannot watch the way Claude Code reads its input. It now gives it the terminal device
  itself.
- If the sign-in still does not finish, the installer says how to get the token from a
  new Terminal window and paste it in.

## Uninstall steps that paste cleanly
- The uninstall commands in the README and `INSTALL.md` contained comment lines. Terminal
  on a Mac (zsh) does not accept those when they are pasted: it showed errors, and one
  line could pass stray words to a delete command. Each step is now its own block with
  its explanation above it.
- The step that deletes the wiki no longer uses an example folder name that could miss
  yours. It deletes the Wiki folder, or says how to delete one wiki of several, and a
  final line checks that nothing is left.

## A richer wiki from the model on this Mac
- The model on this Mac now builds the same kinds of pages as Claude: a summary of each
  document; a page for each company, person and product it names, with its current facts,
  how it relates to the others and the documents it appears in; topic pages (tax,
  contracts, banking, month-end close, ...); decision pages; and an "At a glance" block on
  the overview page.
- Every figure, date and number is checked against its document before it is written.
  Anything the document does not contain is left out. Links are checked too: one company
  "owns" another only where a document speaks of ownership, who supplies whom follows what
  most documents say, and in a spreadsheet two names are linked only by a row naming both.
- A figure a later document changes (payment terms, a price, a fee, a notice period, an
  address) is caught and put in the Review queue with both values and their documents.
  The page shows the new value with the old one beside it.
- The top of a page is kept up to date by the wiki. If you edit it, the wiki leaves it as
  you wrote it.
- Reading takes longer: a few minutes a document, so a large upload runs overnight. The
  measured comparison with Claude is in `INSTALL.md`.
- The model uses less memory over a long run.

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
