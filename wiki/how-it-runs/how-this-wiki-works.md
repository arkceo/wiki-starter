---
title: How this wiki works
type: concept
tags: [how-it-runs, wiki]
sources: []
status: active
updated: 2026-01-01
---

The wiki is a folder on this Mac (`~/Wiki/<name>`). Claude reads what you drop in, writes
and links the pages, and a local website shows the result. Nothing is published, and no
account other than Anthropic's is involved.

## The two drop folders

| Desktop folder | Real location | What goes in | What happens |
|---|---|---|---|
| **Wiki Intake** | `raw/_intake/` | Documents: PDF, Word, Excel, PowerPoint, images, scans | Converted to text (with OCR for scans), summarised into the right pages, then filed under `raw/<project>/` |
| **Wiki Inbox** | `raw/inbox/` | Update Packets (`.md`) from a Claude conversation | Applied to the affected pages, then moved to `archive/inbox/` |

A document dropped into Wiki Inbox by mistake is moved to Wiki Intake automatically.

## What runs in the background

Two background agents start when you log in:

- **The runner** (`scripts/wiki_runner.sh`) wakes when something lands in a drop folder,
  and every 15 minutes in case it missed one. It waits until files finish copying, then:
  1. converts new documents to Markdown (`scripts/to_markdown.py`, see
     [[raw-to-markdown-conversion]]);
  2. runs Claude on the documents, then on the packets, five packets at a time;
  3. tidies frontmatter, refreshes the [[ingestion-register]] and commits the change to
     the folder's local history (git, on this Mac only);
  4. rebuilds the site and shows a notification.
- **The viewer** (`scripts/wiki_server.py`) serves the site at `http://127.0.0.1:8765`.
  It answers only this Mac, never the network.

Logs are in `~/Library/Logs/wiki-starter/`. **Process Now** in the wiki folder runs the
runner by hand.

## What Claude may do when nobody is watching

Read and write files inside the wiki folder, and move files with `mv` and `mkdir`. It
cannot run other commands, use the internet or touch anything outside the folder. Each
run has a spending cap.

## Privacy

- Documents and the wiki stay on this Mac.
- The text Claude reads while working is sent to Anthropic to be processed.
- Source documents are not in the folder's git history. Back the Mac up with Time Machine.

## Engine updates

**Update Wiki Engine** in the wiki folder downloads the newest engine and replaces only
engine files (scripts, site code, these prompts). Your pages, documents, history and
`HOUSE-RULES.md` are never touched. A changed engine file is backed up first.
