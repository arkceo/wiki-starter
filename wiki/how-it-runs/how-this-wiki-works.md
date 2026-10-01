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
| Documents: PDF, Word, Excel, PowerPoint, images, scans | `raw/_intake/` | Converted to text (with OCR for scans), summarised into the right pages, then filed under `raw/<project>/` |
| Update Packets (`.md`) from a Claude conversation | `raw/inbox/` | Applied to the affected pages, then moved to `archive/inbox/` |

The same page shows every file going through its real stages (upload, convert, read and
write, file), a live line saying what the wiki is doing at that moment, and a log of what
it has done. Files copied straight into those two folders in
Finder are processed the same way.

## What runs in the background

Two background agents start when you log in:

- **The runner** (`scripts/wiki_runner.sh`) wakes when something lands in the queue,
  and every 15 minutes in case it missed one. It waits until files finish copying, then:
  1. takes new documents in batches (8 at a time with Claude, 20 with the model on this
     Mac; `docsPerBatch` in `wiki.config.json`), so a large upload is read in full and
     its first pages appear while the rest are still waiting;
  2. converts each batch to Markdown (`scripts/to_markdown.py`, see
     [[raw-to-markdown-conversion]]) and reads it: Claude, or the model on this Mac
     (`scripts/local_engine.py`, which writes a summary page per document under
     `sources/` and pages for the companies and people it names). Then the packets,
     five at a time with Claude (one page each under `updates/` or `decisions/`). With
     the model on this Mac, Claude is never started and nothing leaves the Mac;
  3. tidies frontmatter, refreshes the [[ingestion-register]] and commits the change to
     the folder's local history (git, on this Mac only);
  4. rebuilds the site and shows a notification.
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
- Source documents are not in the folder's git history. Back the Mac up with Time Machine.

## Engine updates

**Update Wiki Engine** in the wiki folder downloads the newest engine and replaces only
engine files (scripts, site code, these prompts). Your pages, documents, history and
`HOUSE-RULES.md` are never touched. A changed engine file is backed up first.
