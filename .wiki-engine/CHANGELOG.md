# Changelog

Newest first. Shown to the SME after "Update Wiki Engine".

## Fast reading for large uploads
- **Fast reading is now how Claude reads.** Claude reads many documents at once, one
  question each (with Claude Sonnet), and the wiki engine
  checks every answer against the document and writes the pages itself, the way it does
  with the model on this Mac. Figures that are not in a document are left out, and a
  figure that changes what an earlier document said becomes a question in the Review tab
  (for you, or for Jev in Auto-review).
- On our 13 test documents, fast reading took about a minute instead of seven, at under
  a third of the cost, and found everything a careful reader should: every fact, every
  company and person, both contradictions, and no figure that is not in a document.
  Classic reading writes richer prose; fast reading is more complete and more uniform.
- Scans and photos are converted by several processes at once, so OCR keeps up.
- Classic reading, a Claude session per batch that writes the pages itself in richer
  prose, can still be chosen in Settings, under Reading speed. Update Packets and your
  review answers are still applied by a Claude session either way.

## Time left and cost for a large upload
- Once two batches have been read, the Upload page shows about how long the documents
  still waiting will take and when they should be done, under the progress bar.
- With Claude it also shows about what the whole upload will cost, and what has been
  spent so far. With an API key that is your bill. With a Claude plan it is the same work
  valued at API prices: the plan covers it within its usage limits, with no bill per
  document. The model on this Mac costs nothing, so only the time is shown.
- Both are measured, never guessed: they come from how long the last batches took and
  what Claude reported they cost, read the way the wiki reads them (the first batch
  alone, then several at once). Hover over the line to see the pace it is based on.
- After a run, the line says how long it took and what it cost.

## Photos with no words get a description
- With Claude, a photo is a document even when there are no words in it: a delivery, a
  site visit, a product, a whiteboard drawing. It gets a page saying what the photo
  shows and when it was taken, linked to what it concerns, instead of ending up in
  Needs review.
- The description is text, so Jev can judge a question about the photo too.
- People are described, never named from their face: a name comes only from text in
  the photo, another document or your note. A photo's location is never read.
- A photo that may not be a business record raises a review question; "set it aside"
  moves it to `raw/_set-aside/`.
- When Claude reads several batches at once, two sessions can no longer replace each
  other's new pages: a page that already exists can only be added to.
- Your company's name from Settings now reaches Claude, so it can tell your business from
  the other companies in a document.

## A Review tab, Settings, and photos that are read
- **Review tab**, next to Files on the Upload page, with a count of open questions.
  Anything that needs your decision becomes a question with answers to choose from: two
  documents that disagree, an unclear fact, or a file that could not be read after two
  tries. Every **Needs review** label and log line links to its question. Choose an
  answer or write your own; the next run applies it to the wiki. A file that could not be
  read can be read again with your note saying what it is, or set aside in
  `raw/_set-aside/`. Nothing is deleted.
- **Auto-review with Jev** (optional, Claude only): save a TypeSafe API key in Settings
  and Jev, TypeSafe's decision model, shows how likely each answer is. In Auto-review,
  Jev's answer is applied only when the documents themselves settle the question (Jev
  checks this first) and its confidence reaches your bar (70% to start). A question that
  needs your own knowledge or decision always waits for you: in testing, Jev was
  confident about such questions too, and sometimes wrong. Questions about one document are sent together, and
  documents at the same time. Without a key, nothing is sent to TypeSafe.
- **Settings** button, top right of the Upload page: reading speed (1 slowest, 3
  moderate, 6 fastest batches at once), review mode and bar, the Jev key (kept in the
  Keychain), title, company name, and batch size, upload limit and spending cap.
- **Photos are read.** Before, a photo became an empty text file and was set aside after
  two tries. Photos are now read with OCR, turned upright first, and iPhone HEIC photos
  are converted. With Claude, photos and poor scans are also looked at as pictures.
- **Pages update by themselves.** An open wiki page reloads when a new version is
  built; a page you are reading shows a button instead, so you never lose your place.

## Claude reads three batches at once
- Claude now reads up to 3 batches of documents at the same time. The first batch runs
  alone, then the rest go three at a time. Set `parallelBatches` in `wiki.config.json`
  (1 to 6; 1 reads one batch at a time as before).
- Documents are converted to text in the background ahead of the reading, so a scan's
  OCR no longer holds up Claude.
- The site is refreshed every few minutes during a long run, so the first pages appear
  while the rest are still being read.
- A large upload uses a subscription's usage, or an API key's spending, faster. The
  total stays about the same.

## A sign-in token pasted over two lines works
- Claude Code prints the subscription token over two lines in a narrow Terminal window.
  The installer read only the first line, rejected the token, and the second half then
  ran as a command. It now joins a paste that arrives as two lines.
- If a token still does not work, the installer asks for the rest of it, or for the whole
  token again, up to three tries, instead of stopping.

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
