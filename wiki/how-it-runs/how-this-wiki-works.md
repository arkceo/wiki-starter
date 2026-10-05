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

Click **Upload** at the top right of any page. Drop anything onto the Upload page, files
or whole folders, or click its box to choose files; it sorts them into a queue inside the
wiki folder:

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
Anyone who can open the wiki on this Mac can see them.

## Asking the wiki

With Claude, click **Ask** beside Upload on any page and type a question. The wiki finds
the pages that matter on this Mac and sends only those to Claude. Every sentence of the
answer is checked against its page before it is shown, with a numbered link to each page.
If the pages do not say, the answer says so, and what to upload. A question costs about four
US cents, and never more than US$0.25. **Save to the wiki** files a good answer as
its own page; **Correct it** sends what is right, and the pages are corrected on the next
run. The last 20 questions stay on the Upload page's **Ask** tab.

## Questions for you: the Review tab

When something needs a decision (two documents disagree, a fact is uncertain, or a file
could not be read after two tries), it becomes a question in the Upload page's
**Review** tab, with a few answers to choose from. Every **Needs review** label and log
line links to its question. Answer by choosing one, or write your own; the next run
applies it to the wiki. Each question links to its document wherever it is now, and a
question about a picture shows the picture. A file that could not be read can be sent back to be read again
(with a note from you saying what it is), set aside in `raw/_set-aside/`, or left in
`raw/_needs-review/`. Nothing is ever deleted. Each question is also recorded in the
[[_review|Review queue]].

**Jev**, TypeSafe's decision model, can judge the answers for you. The installer asks
for a TypeSafe API key (or save one later in **Settings**, top right of the Upload page),
and each answer shows how likely Jev thinks it is. In **Manual**, you choose. In
**Auto-review**, Jev's answer is applied when the documents settle the question and Jev
is at least 70% sure, and each decision is logged in the Review tab, with how Jev scored
every answer; **Change this answer** opens the question again for you (not for a file that
could not be read: that answer moved the file). A question that
needs your own knowledge or decision always waits for you. Jev is used only when Claude
reads your documents.

## Actions

When an answer means something must be done outside the wiki (a return to correct, an
amount to chase, a licence to renew), Claude drafts it as an **action** in the Upload
page's **Actions** tab when it applies the answer: why, the steps, and an instruction
brief for each kind of doer: an AI agent with a browser (which prepares everything and
stops to ask you before submitting or paying anything), a member of your staff, or an
outside professional (your tax agent, company secretary, accountant or lawyer). Choose
who does it, copy or download the brief, and mark it done. Actions are kept in
`actions/`.

## Settings

**Settings**, top right of the Upload page, sets:

- **Reading speed**: how many documents Claude reads at once, from 3 to 18 (the fastest,
  and the default). Speed changes how soon an upload is done, not what it costs.
- **Performance mode**: Economical, Moderate or Maximum performance (the default). Each
  sets how many documents go in a batch, the largest file the Upload page takes, and a
  spending cap per batch. A document costs the same in every mode; the cap stops a batch
  costing more than expected, and the documents it did not reach wait for the next run.
- How review questions are answered, the Jev key, the wiki's title and your company's name.

## What runs in the background

Two background agents start when you log in:

- **The runner** (`scripts/wiki_runner.sh`) wakes when something lands in the queue,
  and every 15 minutes in case it missed one. It waits until files finish copying, then:
  1. takes new documents in batches, as large as the performance mode says (100 at a
     time with Claude in Maximum performance; `scripts/wiki_settings.py` has the
     figures). Claude reads up to 18 documents at the same time (reading speed,
     `parallelBatches`; the first document always goes alone), and the site is
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
  3. tidies frontmatter, sorts new pages into the menu (`scripts/wiki_menu.py`; the
     sections are in `engine/menu.json`), refreshes the [[ingestion-register]] and commits
     the change to the folder's local history (git, on this Mac only);
  4. rebuilds the site and shows a notification. Open wiki pages show the new version
     by themselves (a page you are reading offers a button instead of jumping).
- **The viewer** (`scripts/wiki_server.py`) serves the site at `http://127.0.0.1:8765`
  and the Upload page next to it, on port 8766. It answers only this Mac, never the
  network, and only the Upload page it served can add files.

Logs are in `~/Library/Logs/wiki-starter/`; the Upload page shows the same activity in
plain language. **Process now** on the Upload page runs the runner straight away. If a run
shows no progress for 15 minutes, the Upload page offers **Restart processing**: what is
filed stays filed, and the rest is read again.

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
