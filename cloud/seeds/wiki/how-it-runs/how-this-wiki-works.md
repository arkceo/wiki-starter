---
title: How this wiki works
type: concept
tags: [how-it-runs, wiki]
sources: []
status: active
updated: 2026-10-07
---

This wiki is kept for you by oeru in Malaysia, in a private place of its own: its files
are encrypted, and no other business can reach them. Claude reads what you upload and
writes and links the pages, and the site you are reading shows the result. Nothing is
published: the wiki opens only through your oeru sign-in. Claude's reading is paid from
your oeru credits.

## Adding documents and notes

Click **Upload** at the top right of any page. On the page it opens, drop files onto the
box or click it to choose them, up to 20 at a time and 100 MB each. They go straight into
the wiki's own storage, into a queue:

| What you upload | Queue | What happens |
|---|---|---|
| Documents: PDF, Word, Excel, PowerPoint, photos, scans | `raw/_intake/` | Converted to text (with OCR for scans and photos), summarised into the right pages, then filed under `raw/<project>/` |
| Update Packets (`.md`) from a Claude conversation | `raw/inbox/` | Applied to the affected pages, then moved to `archive/inbox/` |

While they upload and while they are read, a live line shows what is happening: the
step, how many files are still to read, and how long it has taken so far.

## The menu

The menu on the left sorts the wiki into sections: **Company Profile**, **Marketing**,
**Sales**, **Customer Service**, **Human Resources**, **Operation**, **Legal**,
**Account** and **Finance**, each with its categories. Company Profile holds the business
itself (with its directors and shareholders) and everyone it deals with: employees,
contractors, customers, suppliers, its company secretary, accountant, auditor, tax agent
and lawyer, government bodies, banks and financiers, landlords and insurers. Only
sections and categories that have pages show, and each can be folded away.

The menu is only a way in: pages stay where they are, under `companies/`, `sources/`,
`finance-legal/` and so on, and a page can sit in several categories at once (a firm that
is both your accountant and your tax agent shows under both). Each page says where it
belongs in a `menu:` line at its top, set when its document is read, from what the
document says about each company and person. The wiki only ever adds the categories it
can tell from the documents. To place a page yourself, edit that line and add
`menu_auto: false` below it: the wiki then leaves the page's line as you set it.
Products, projects, decisions and updates keep their own groups below the sections.

An employee's page holds their details as the documents give them (IC or passport number,
EPF and SOCSO numbers, address, phone, date of birth, salary), so they can be looked up.
Anyone who can sign in to your oeru account can see them.

## How the reading runs

Nothing runs while you are not using the wiki. When files arrive, a machine is made for
that one job: it takes the wiki from storage, reads what waits in the queues, rebuilds
the site, puts everything back and is deleted. Only one job runs for your wiki at a time;
files that arrive meanwhile are read straight after. A job stops taking on new documents
after 40 minutes, and whatever is left waits for the next one.

Ask, the Review tab, Actions and Settings belong to the wiki's own Upload page on a Mac.
On oeru they are being brought to the Upload page, one at a time.

## What Claude may do when nobody is watching

Read and write files inside the wiki, and move files with `mv` and `mkdir`. It cannot run
other commands, use the internet or touch anything outside the wiki. Each run has a
spending cap.

## Privacy

- Your documents and the wiki are stored in Malaysia (AWS, Asia Pacific (Malaysia)),
  encrypted, and kept apart from every other business's.
- The text Claude reads while working is sent to Anthropic, in the United States, to be
  processed.
- oeru runs the wiki; Amazon Web Services hosts it. Neither reads your
  wiki.
- Source documents are not in the wiki's history, only in its storage.

## The engine

The wiki runs on wiki-starter, whose source is public. oeru builds the machine that reads
your files from a published wiki-starter release, and the workspace page in oeru says
which one, so anyone can check the code that touches your documents. Your pages,
documents, history and `HOUSE-RULES.md` are never touched by an engine update, and a
starter page you have edited is never replaced.
