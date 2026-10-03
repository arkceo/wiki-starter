---
title: How this wiki works
type: concept
tags: [how-it-runs, wiki]
sources: []
status: active
updated: 2026-01-01
---

The wiki is a folder on this Mac (`~/Wiki/<name>`). Claude, or a model running on this
Mac, reads what you upload and writes and links the pages, and a local website shows the
result. Nothing is published. Claude needs an Anthropic account; the model on this Mac
needs none (`"engine"` in `wiki.config.json` says which one reads).

## Adding documents and notes

Click **Upload** at the top right of any page. Drop files or whole folders onto the
Upload page; it sorts them into a queue inside the wiki folder:

| What you upload | Queue | What happens |
|---|---|---|
| Documents: PDF, Word, Excel, PowerPoint, photos, scans | `raw/_intake/` | Converted to text (with OCR for scans and photos), summarised into the right pages, then filed under `raw/<project>/` |
| Update Packets (`.md`) from a Claude conversation | `raw/inbox/` | Applied to the affected pages, then moved to `archive/inbox/` |

The same page shows every file going through its real stages (upload, convert, read and
write, file), a live line saying what the wiki is doing at that moment, and a log of what
it has done. Files copied straight into those two folders in
Finder are processed the same way. With Claude, photos and scans are also looked at as
pictures, so a receipt photographed on a phone is read properly, and a photo with no
words in it gets a page saying what it shows and when it was taken. People are
described, never named from their face, and a photo's location is never read.

## Questions for you: the Review tab

When something needs a decision (two documents disagree, a fact is uncertain, or a file
could not be read after two tries), it becomes a question in the Upload page's
**Review** tab, with a few answers to choose from. Every **Needs review** label and log
line links to its question. Answer by choosing one, or write your own; the next run
applies it to the wiki. A file that could not be read can be sent back to be read again
(with a note from you saying what it is), set aside in `raw/_set-aside/`, or left in
`raw/_needs-review/`. Nothing is ever deleted. Each question is also recorded in the
[[_review|Review queue]].

**Jev**, TypeSafe's decision model, can judge the answers for you. Save a TypeSafe API key
in **Settings** (top right of the Upload page) and each answer shows how likely Jev
thinks it is. In **Auto-review**, Jev's answer is applied when the documents settle the
question and its confidence is at least your bar (70% unless you change it). A question
that needs your own knowledge or decision always waits for you. Jev is used only when
Claude reads your documents.

**Settings** also sets the reading speed (how many batches Claude reads at once), the
wiki's title and your company's name.

## What runs in the background

Two background agents start when you log in:

- **The runner** (`scripts/wiki_runner.sh`) wakes when something lands in the queue,
  and every 15 minutes in case it missed one. It waits until files finish copying, then:
  1. takes new documents in batches (8 at a time with Claude, 20 with the model on this
     Mac; `docsPerBatch` in `wiki.config.json`). Claude reads up to 3 batches at the same
     time (`parallelBatches`; the first batch always runs alone), and the site is
     refreshed every few minutes, so the first pages appear while the rest are being read;
  2. converts the batches to Markdown ahead of the reading (`scripts/to_markdown.py`, see
     [[raw-to-markdown-conversion]]) and reads each one: Claude, or the model on this Mac
     (`scripts/local_engine.py`). The model on this Mac answers focused questions
     about each document; code keeps only figures the document contains and writes a
     summary page under `sources/`, a page for each company, person and product it
     names (with its current facts and how it relates to the others), topic pages under
     `finance-legal/` and `how-it-runs/`, and decision pages. A figure that changes
     (payment terms, a price, a fee, a notice period, an address) goes to the
     [[_review|Review queue]] with both values. Then the packets, five at a time with
     Claude (one page each under `updates/` or `decisions/`). With the model on this
     Mac, Claude is never started and nothing leaves the Mac;
  3. tidies frontmatter, refreshes the [[ingestion-register]] and commits the change to
     the folder's local history (git, on this Mac only);
  4. rebuilds the site and shows a notification. Open wiki pages show the new version
     by themselves (a page you are reading offers a button instead of jumping).
- **The viewer** (`scripts/wiki_server.py`) serves the site at `http://127.0.0.1:8765`
  and the Upload page next to it, on port 8766. It answers only this Mac, never the
  network, and only the Upload page it served can add files.

Logs are in `~/Library/Logs/wiki-starter/`; the Upload page shows the same activity in
plain language. **Process now** on the Upload page runs the runner straight away.

## What Claude may do when nobody is watching

Read and write files inside the wiki folder, and move files with `mv` and `mkdir`. It
cannot run other commands, use the internet or touch anything outside the folder. Each
run has a spending cap.

## Privacy

- Documents and the wiki stay on this Mac.
- With Claude, the text Claude reads while working is sent to Anthropic to be processed.
  With the model on this Mac, nothing leaves the Mac.
- With a TypeSafe key saved (Claude only), each review question, its answers, and the
  parts of the wiki and the document it is about are sent to TypeSafe to be scored. The
  key is kept in the Keychain.
- Source documents are not in the folder's git history. Back the Mac up with Time Machine.

## Engine updates

**Update Wiki Engine** in the wiki folder downloads the newest engine and replaces only
engine files (scripts, site code, these prompts). Your pages, documents, history and
`HOUSE-RULES.md` are never touched. A changed engine file is backed up first.
