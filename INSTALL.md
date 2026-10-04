# Installing the wiki

## Before you start

- A Mac with **macOS 13 (Ventura) or newer** and an **administrator** account. A Mac mini
  that stays on and logged in is ideal, because the wiki updates in the background.
- About **3 GB** of free disk space for the tools, plus room for your documents.
- One of these to read your documents:
  - **Claude** (recommended, the richest wiki): an **Anthropic** account, either a
    **Claude Pro or Max subscription** or an **API key** from
    [console.anthropic.com](https://console.anthropic.com/settings/keys) (set a monthly
    spend limit there); or
  - **This Mac only**: no account. A Mac with Apple silicon and **16 GB of memory**, and
    about 6.2 GB more disk space for the model.
- Optional, with Claude: a **TypeSafe API key** from
  [console.typesafe.ai](https://console.typesafe.ai) for **Jev**, the review judge
  (Auto-review). It can also be added later.

No GitHub account, no website and no other service is needed.

## Install

Open **Terminal** (Applications → Utilities) and paste:

```sh
curl -fsSL https://raw.githubusercontent.com/arkceo/wiki-starter/main/install.sh | bash
```

Paste the line rather than downloading the file: macOS blocks downloaded scripts from
opening with a double-click.

The installer works through eight steps and prints a tick for each. The first run takes
10–20 minutes, mostly downloading tools. It asks for:

1. **Your Mac password**, once, to install [Homebrew](https://brew.sh). Skipped if
   Homebrew is already installed.
2. **Company name** and **wiki title**. The wiki goes in `~/Wiki/<title>`.
3. **Who reads your documents:** 1 for Claude, 2 for a model on this Mac.
4. With Claude, an **Anthropic sign-in**:
   - *Subscription:* a browser window opens. Sign in, approve, and copy the token that
     Terminal shows (it starts with `sk-ant-oat`). Paste it when asked.
   - *API key:* paste the key when asked.
   Either way it is stored in your **macOS Keychain**, not in a file.
   With the model on this Mac there is no sign-in: the installer downloads the model
   from Hugging Face, checks it against its published checksum, and starts it once to
   make sure it answers.
5. With Claude, a **TypeSafe API key** for **Jev**, the review judge (optional). Jev
   scores the answers to each review question, and in Auto-review answers the ones the
   documents themselves settle. The installer opens console.typesafe.ai; paste the key
   when asked, or press Return to skip and add one later in **Settings** on the Upload
   page. Jev checks the key: one TypeSafe refuses can be pasted again (after three tries
   the step is skipped), and one that cannot be checked just then is stored anyway. A
   key is stored in the **Keychain** and turns **Auto-review** on, unless you already
   chose a review mode. With the model on this Mac this step asks nothing: nothing
   leaves the Mac.

To switch between Claude and the model on this Mac later, run the install line again and
choose the other option. Your pages and documents stay as they are.

If anything fails, run the same line again. Finished steps are skipped.

### Answers in advance

To install without the questions, give the answers as environment variables in front of
`bash` in the install line, for example `curl -fsSL … | WIKI_JEV=skip bash`:

| Variable | Answers |
|---|---|
| `WIKI_COMPANY`, `WIKI_TITLE` | the company name and the wiki title |
| `WIKI_ENGINE` | who reads: `claude` or `local` |
| `WIKI_AUTH` | the Anthropic sign-in: `subscription`, `apikey` or `skip` |
| `WIKI_OAUTH_TOKEN`, `WIKI_API_KEY` | the subscription token, or the API key |
| `WIKI_TYPESAFE_KEY` | the TypeSafe API key for Jev |
| `WIKI_JEV=skip` | no TypeSafe key for now |
| `WIKI_SKIP_SMOKE_TEST=1` | no welcome-note test at the end |

A key typed into the install line stays in Terminal's history; paste it at the prompt
when you can.

## What gets installed

| Where | What |
|---|---|
| Homebrew | `git`, `node`, `python@3.12`, `ocrmypdf` (with Tesseract and Ghostscript); `llama.cpp` with the model on this Mac |
| `~/.local/bin/claude` | Claude Code (with Claude only) |
| `~/Library/Application Support/wiki-starter/models/` | the model (with the model on this Mac only) |
| `~/Wiki/<title>/` | the wiki: engine, your pages, your documents, local history |
| `~/Library/LaunchAgents/local.wiki-starter.<title>.*.plist` | the runner and the viewer |
| `~/Library/Logs/wiki-starter/` | logs (no secrets) |
| Desktop | **Open Wiki** |
| Keychain | your Anthropic sign-in and, if you gave one, your TypeSafe key, under "wiki-starter" (with Claude only) |

## Using it

- **Add documents and notes** → click **Upload** at the top right of the wiki and drop
  files or whole folders onto the page. Each file shows its real stages (upload, convert,
  read and write, file); a live line says what the wiki is doing at that moment (which
  file, which step, how long); and a log keeps what it has done.
- **Update Packets** are uploaded the same way. To get them, upload the `skills/packet/`
  folder (zip it first) to your Claude account in its Skills settings,
  then say "wrap up" at the end of a conversation.
- **Read** → double-click **Open Wiki**, or visit `http://127.0.0.1:8765`.
- **Ask** → open the wiki folder in the Claude desktop app, or run `claude` in Terminal
  inside it, and ask. Claude answers from the wiki with citations.
- **Your rules** → edit `HOUSE-RULES.md` in the wiki folder: who you are, how to file
  things, what never to record. Claude follows it on every run.
- **Process now** on the Upload page runs the runner immediately.
- **Review** → questions that need your decision wait in the Upload page's **Review**
  tab, each with answers to choose from. Every **Needs review** label links to its
  question. The *Review queue* page keeps the full record.
- **Auto-review (optional, Claude only)** → give the installer a TypeSafe API key, or save
  one later in **Settings** (top right of the Upload page). Jev, TypeSafe's decision
  model, then scores each answer, and in Auto-review applies its answer when the
  documents settle the question and it is at least 70% sure, logging each decision in the
  Review tab, where you can change it. A key given to the installer turns Auto-review on;
  switch it in the Review tab. Questions that need your own knowledge or decision always
  wait for you. TypeSafe charges per token of the text it is sent: a small fraction of a
  cent per question.
- **Actions** → when an answer means something must be done outside the wiki, Claude
  drafts it in the Actions tab with an instruction brief for an AI agent, a member of
  staff or an outside professional.
- **Settings** → reading speed (3 to 18 documents at once; the fastest is the default),
  performance mode (Economical, Moderate, Maximum performance: batch size, largest upload
  and spending cap per batch), review mode, the Jev key, title and company name. They are
  saved in `wiki.config.json`; `scripts/wiki_settings.py` has the figures.
- **Cached reading instructions** (on) → the reading instructions, the same for every
  document, go where Claude can reuse them from its cache; `"fast": {"cacheInstructions":
  false}` turns it off.
- **Page overviews** → the short overview at the top of a page is written again only when
  what it summarises changed, and never over one you edited. `"overviews"` in
  `wiki.config.json` holds the switches: `skipUnchanged` (on), `skipImmaterial` (off: skip
  when only another document in a role the page already had came in), `jev` (off: with
  Claude, a TypeSafe key and an Anthropic API key, Jev judges whether an overview still
  holds; it is sent that overview and the page's facts) and `jevBar` (0.9). A new price,
  fee or payment term always brings a fresh overview.
- **A cheaper reader where it is enough** (off) → with Claude, short text invoices,
  receipts, quotations and orders are read by a lighter, cheaper model (`fastModel`, Haiku,
  its thinking off), and everything else by the usual one. `"fast": {"routing": {"enabled":
  true}}` in `wiki.config.json` turns it on. With `"jev": true` and a TypeSafe key, Jev
  judges the documents code cannot place from their start (`jevBar`, 0.7) instead of
  sending them to the usual model. Code checks every answer of the lighter model
  and has one that missed the document's figures read again by the usual one
  (`minCoverage`, 0.5); the runner log counts how many were.

## Costs

- **Model on this Mac:** free. It uses the Mac's memory and power while it reads, and
  frees the memory when it is done.
- **Subscription:** processing counts against your plan's usage limits. Claude reads up
  to 18 documents at once by default, so a large upload uses them up faster, in less
  time. On a smaller plan, lower **Reading speed** in Settings.
- **API key:** you pay per use. A typical document costs cents; a long scanned contract
  can cost more. Each batch has a spending cap set by the performance mode (US$2, US$5
  or US$25 per batch), so a large upload costs in proportion to its size; documents a
  batch did not reach are read in another run that starts right away. Reading documents
  side by side changes the speed, not the total. A file that fails twice is set aside
  instead of retried.
- Everything else is free.

## Privacy

- Documents and the wiki stay on this Mac, in `~/Wiki/<title>`.
- The site is served only to this Mac (`127.0.0.1`). Other devices cannot open it.
- The wiki never uses your local network: it does not look for, or connect to, other
  devices. If macOS asks whether "Python" may find devices on your local network, choose
  **Don't Allow**; everything keeps working.
- With Claude, the text Claude reads while working is sent to Anthropic to be processed.
  With the model on this Mac, nothing leaves the Mac: Claude is never started and no
  Anthropic account is used.
- Jev is off until a TypeSafe API key is saved, by the installer or in Settings, and is
  never used with the model on this Mac. With a key, each review question, its answers, and the parts of
  the wiki and the document it is about are sent to TypeSafe to be scored. If you turn on
  Jev's overview check, a page's overview and its facts are sent too; if you turn on
  choosing a reader per document, the file name and start (up to 12,000 characters) of
  each document code cannot place itself. Nothing else.
  The key is kept in the Keychain, not in a file.
- Source documents are not kept in the wiki's git history. **Turn on Time Machine.**

## Claude or the model on this Mac?

| | Claude | Model on this Mac |
|---|---|---|
| Account | Anthropic (subscription or API key) | none |
| What leaves the Mac | the text being read | nothing |
| Pages | written and cross-linked with judgement | the same kinds of pages: a summary per document; a page for each company, person and product with its current facts, links and documents; topic pages (tax, contracts, banking, month-end close, ...); decision pages |
| Figures | read with judgement | every figure, date and number checked against its document before it is written; anything not found there is left out |
| Contradictions | noticed and put in the Review queue | a figure a later document changes (payment terms, a price, a fee, a notice period, an address) is caught by code and put in the Review queue with both values; other contradictions only when plain |
| Writing | natural, with judgement about what matters | correct but plainer, and now and then it misreads a role or how two parties relate |
| Speed | a few minutes per batch | a few minutes per document (about 2 to 4 on Apple silicon); a large upload runs overnight |
| Asking questions in Claude | yes | needs Claude |

**How they compare, measured.** The model on this Mac read 12 sample documents of a
fictional small business (contracts, invoices, a quotation and the engagement letter that
changed its fee, board minutes, a tax return, a bank reconciliation, a ledger and a
checklist as spreadsheets), an Update Packet, and a supplier page written by hand. The
Claude column is a reference wiki written for the same documents by Claude's own rules for
this wiki (`CLAUDE.md`); "before" is the previous version of the model on this Mac:

| Measured | Claude | Model on this Mac | Before this version |
|---|---|---|---|
| Facts captured (of 107) | 107 | 107 | 96 |
| Companies, staff and products with a page (of 14) | 14 | 14 | 13 |
| Links between them (of 13) | 13 | 13 | 0 |
| Registration, tax and bank numbers on the right page (of 8) | 8 | 8 | 3 |
| Planted contradictions caught (of 2) | 2 | 2 | 0 |
| False alarms in the Review queue | 0 | 0 | 0 |
| Numbers not in any document | 0 | 0 | 1 |
| Topic and decision pages expected (of 9) | 9 | 9 | 1 |

The counts do not measure the writing. Read side by side, the model on this Mac still
mislabels a figure now and then (one quarter's revenue given for another), and its
overviews are plainer and sometimes misstate how two parties deal with each other. The
set took 143 minutes on a test machine's processor, without a graphics chip; Apple
silicon is several times faster.

## Updating the engine

Double-click **Update Wiki Engine** in the wiki folder. It downloads the newest engine and
replaces engine files only: scripts, site code, prompts, `CLAUDE.md`. It never touches
`wiki/`, `raw/`, `archive/`, your projects under `generated/`, `index.md`, `log.md`,
`HOUSE-RULES.md` or `wiki.config.json`. An engine file you edited is backed up to
`.wiki-engine/backup/` first. Undo an update with `git revert HEAD` in the wiki folder.
The starter pages that explain how to use the wiki (the home page and *How this wiki
works*) follow the engine's instructions only while nobody has edited them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Nothing seems to happen after uploading a file | It is probably working: a batch of PDFs takes 5–15 minutes. The Upload page's live log shows each step; the full log is `~/Library/Logs/wiki-starter/runner.log`. If no "Wiki is working" notice appeared, allow notifications for *Script Editor* in System Settings → Notifications. |
| "Wiki needs attention: Claude could not run: your Anthropic API credit has run out" | The API key has no credit left. Add credit at console.anthropic.com (Billing), then press **Process now** on the Upload page. |
| "Wiki needs attention: Claude could not run: … usage limit …" | Your plan's limit is used up for now. Nothing to do: the documents wait, and a run after it resets reads them. |
| "Wiki needs attention: Claude could not run: it is not signed in" | The sign-in expired or was revoked. Run the install line again and choose to replace the sign-in. |
| A file says **Needs review** | It could not be read twice. Click the label to open its question in the Review tab: read it again (with a note saying what it is), set it aside, or leave it. |
| Scanned PDF summarised badly | OCR quality depends on the scan. The original is always kept and linked from the page. |
| Open Wiki shows "being built" | The first build takes a minute or two. The page opens the wiki by itself when it is ready. |
| The Upload page says "Forbidden" | The viewer restarted (an engine update does that). Reload the page. |
| The wiki stopped updating after a restart | Log in: background agents only run while a user is signed in. Consider automatic login on a dedicated Mac mini. |
| Anything else | `~/Library/Logs/wiki-starter/runner.log` and `install.log` |

## Removing it

Paste each block into Terminal on its own. First stop the wiki and remove its background
processes:

```sh
for a in ~/Library/LaunchAgents/local.wiki-starter.*.plist; do
  launchctl bootout "gui/$(id -u)" "$a" 2>/dev/null; rm -f "$a"
done
```

Then remove the Anthropic sign-in, the TypeSafe key, the model on this Mac (if it was
downloaded), llama.cpp and the Desktop shortcuts:

```sh
security delete-generic-password -s wiki-starter -a claude-oauth-token 2>/dev/null
security delete-generic-password -s wiki-starter -a anthropic-api-key 2>/dev/null
security delete-generic-password -s wiki-starter -a typesafe-api-key 2>/dev/null
rm -rf ~/Library/"Application Support"/wiki-starter
brew uninstall llama.cpp 2>/dev/null
rm -f ~/Desktop/"Open Wiki.webloc" ~/Desktop/"Wiki Inbox" ~/Desktop/"Wiki Intake"
```

Your wiki folder in `~/Wiki/` is left in place. To delete it as well, see *Uninstall* in
`README.md`.
