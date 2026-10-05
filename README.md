# wiki-starter

A company wiki that writes itself and lives only on your Mac.

Click **Upload**, drop in documents and notes, and watch the progress live. Claude, or a
model that runs on the Mac itself, reads them, writes and cross-links the wiki pages, and
files the originals. You read the result in your browser. Nothing is published.

## Before you start

- A Mac on macOS 13 (Ventura) or newer, with an **administrator** account that stays
  logged in. The wiki only works while someone is signed in, so a Mac mini that stays on
  is ideal.
- Choose who reads your documents:
  - **Claude** (recommended): you need a Claude Pro or Max subscription, or an Anthropic
    API key from console.anthropic.com with a monthly spend limit set.
  - **This Mac only**: a model on the Mac reads everything. Nothing leaves the Mac, no
    account is needed and Claude is never used, but the Mac must have Apple silicon and
    **16 GB of memory**. It builds the same kinds of pages as Claude and checks every
    figure against its document, more slowly (a few minutes a document) and with plainer
    writing. `INSTALL.md` has the measured comparison.
- Optional, with Claude: a **TypeSafe API key** from console.typesafe.ai for Jev, which
  can answer the review questions your documents settle. You can also add it later.
- About 3 GB of free disk space (about 9 GB with the model on this Mac), and an internet
  connection.

**Already installed?** You don't need to reinstall. Double-click **Update Wiki Engine** in
the wiki folder. For a clean start instead, uninstall first (last section).

## Install

Open **Terminal** (Applications → Utilities), paste this line and press Return:

```sh
curl -fsSL https://raw.githubusercontent.com/arkceo/wiki-starter/main/install.sh | bash
```

The installer has 8 steps, takes 10–20 minutes the first time, and shows ✓ as each step
finishes. It asks for:

1. **Your Mac password**, once, to install Homebrew. This is skipped if Homebrew is
   already installed.
2. **Company name** and **wiki title**. The title becomes the folder name, so
   "Company Wiki" becomes `~/Wiki/company-wiki`.
3. **Who reads your documents:** type **1** for Claude or **2** for This Mac only.
4. What happens next depends on that choice:
   - **Claude with a subscription:** a browser window opens. Sign in and approve, then
     copy the token Terminal shows (it starts with `sk-ant-oat`) and paste it.
   - **Claude with an API key:** paste the key.
   - **This Mac only:** there's no sign-in. The installer downloads the model
     (Qwen3.5-9B, about 6.2 GB; it resumes if interrupted), checks it, and starts it once
     to make sure it works.
5. **Jev, the review judge** (Claude only, optional): paste a TypeSafe API key (the
   installer opens console.typesafe.ai), or press Return to skip and add one later in
   **Settings**. Jev checks the key first; if TypeSafe refuses it you can paste it again.
   With a key, **Auto-review** starts on. With This Mac only, nothing is asked: nothing
   leaves the Mac.

It finishes by processing a welcome note as a test and opening the wiki in your browser.
If macOS shows **Background Items Added**, leave it allowed.

**If any step fails, paste the same line again.** Finished steps are skipped.

To install without the questions, the answers can be given in advance as environment
variables (`WIKI_ENGINE`, `WIKI_TYPESAFE_KEY`, `WIKI_JEV=skip` and others); `INSTALL.md`
lists them.

## Daily use

- **Read the wiki:** double-click **Open Wiki** on the Desktop, or go to
  `http://127.0.0.1:8765`.
- **The menu** on the left sorts the wiki into sections: Company Profile (the business
  itself, its directors and shareholders, and everyone it deals with: employees,
  contractors, customers, suppliers, company secretary, accountant, auditor, tax agent,
  lawyer, government bodies, banks and financiers, landlords, insurers), then Marketing,
  Sales, Customer Service, Human Resources, Operation, Legal, Account and Finance, each
  with its categories. Only the parts that have pages show, and each folds away. The
  menu is only a way in: pages keep their place in the folder, and one page can show
  under several categories (a firm that is your accountant and your tax agent). Each page
  says where it belongs in a `menu:` line at its top. The wiki only adds categories; to
  place a page yourself, edit that line and add `menu_auto: false` under it. A wiki
  from an earlier version is sorted once, by itself, after the update; with Claude, Claude
  places the pages the rules cannot (titles and first lines only, under the spending cap).
- **Employee pages** hold the person's details as the documents give them (IC or passport
  number, EPF and SOCSO numbers, address, phone, date of birth, salary), for looking them
  up. Anyone who can open the wiki on that Mac sees them.
- **Add documents or notes:** click **Upload** at the top right of any wiki page. Drop in
  files, whole folders or Update Packets, or use **Choose files**. Your originals stay
  where they are.
- **Watch progress:** the Upload page shows each file moving through its real stages
  (upload, convert, read and write, file), an "n of m done" count, a live line saying what
  the wiki is doing at that moment, and a log of what it has done. You get a notification
  when the wiki has been updated, and open wiki pages show the new version by themselves.
- **Large uploads** are read in batches, as large as the performance mode says (100
  documents each with Claude in Maximum performance, the default), with Claude reading up
  to 18 documents at the same time. Documents are converted to text ahead of the reading,
  and the site is refreshed every few minutes, so the first pages appear while the rest
  are still being read. A file that crashes or hangs the converter is marked and set
  aside after two tries instead of stopping the rest.
- **Fast reading** (the default with Claude; Settings → Reading speed): Claude reads many
  documents at once, one question each, and the wiki engine checks every answer against
  the document and writes the pages itself, the way it does with the model on this Mac.
  On a set of 13 test documents it read them about seven times faster than the classic
  way (about a minute instead of seven) at under a third of the cost, caught the same
  contradictions, gave every company and person a page, and wrote no figure that is not
  in a document. Classic reading writes richer prose; fast reading is more complete and
  more uniform. Contradictions it finds become questions in the
  Review tab. The spending cap per batch still holds: once Claude's reported cost reaches
  it, no new document is started. Classic reading (a Claude session per batch that writes
  the pages itself, in richer prose) can still be chosen there.
- **Time left and cost:** once two batches have been read, the Upload page shows about how
  long the documents still waiting will take, when they should be done, and, with Claude,
  about what they will cost. Both are worked out from the batches already read, and get
  better as more finish. With an API key the cost is your bill; with a Claude plan it is
  the same work valued at API prices, which the plan covers within its usage limits. The
  model on this Mac costs nothing.
- **Process immediately:** press **Process now** on the Upload page.
- **Ask the wiki** (Claude only): click **Ask** beside Upload on any wiki page and type a
  question ("What are our payment terms with Northwind?"). The wiki finds the pages that
  matter on this Mac, sends only those to Claude, and checks every sentence of the answer
  against its page before showing it, with a numbered link to each page. If the pages do
  not say, it tells you so, and what to upload. A question costs about four US cents
  (US$0.25 at most). **Save to the wiki** files a good answer as a page; **Correct it**
  sends what is right, and the pages are corrected on the next run. The last 20 questions
  stay on the Upload page's **Ask** tab. For longer work, open the wiki folder in the
  Claude desktop app, or run `claude` in Terminal inside that folder.
- **Review:** anything that needs your decision (two documents disagree, a fact is
  unclear, a file could not be read) waits in the Upload page's **Review** tab, with a
  count on the tab. Every **Needs review** label links to its question. Choose an answer
  or write your own; the wiki applies it on its next run. A file that could not be read
  can be read again with your note saying what it is, or set aside. Nothing is deleted.
  Each question links to its document wherever it has been filed, and a question about a
  picture shows the picture.
  The *Review queue* page keeps the full record. With the model on this Mac, a figure a
  later document changes (payment terms, a price, a fee, a notice period, an address) is
  caught by code and listed with both values and their documents.
- **Auto-review with Jev** (Claude only, optional): give the installer a TypeSafe API key,
  or save one later in **Settings**, and Jev, TypeSafe's decision model, scores every
  answer. In **Auto-review** (on from the start with a key given to the installer; switch
  it in the Review tab) Jev's answer is applied when the documents themselves settle the
  question (Jev checks this first) and it is at least 70% sure. Every decision Jev makes
  is logged in the Review tab with how it scored each answer, and **Change this answer**
  opens a question again for you (any answer, yours too, except about a file that could
  not be read: that answer moved the file). A question that needs your own
  knowledge or decision always waits for you.
- **Actions:** when an answer means something must be done outside the wiki (a return to
  correct, an amount to chase), Claude drafts it in the Upload page's **Actions** tab with
  an instruction brief for an AI agent, a member of staff or an outside professional.
  Choose who does it, copy or download the brief, and mark it done.
- **Settings** (top right of the Upload page): reading speed (3 to 18 documents at once;
  18, the fastest, is the default; speed changes how soon an upload is done, not its
  cost), the performance mode (Economical, Moderate or Maximum performance, which set the
  batch size, the largest upload and the spending cap per batch), the review mode, the
  Jev key, the title and your company's name.
- **Photos and scans:** photos (including iPhone HEIC) are read with OCR, and with Claude
  also looked at as pictures, so a receipt photographed on a phone is read properly. A
  photo with no words in it (a delivery, a site, a product) gets a page saying what it
  shows and when it was taken, so it can be searched and judged like any document.
  People in a photo are described, never named from their face, and a photo's location
  is never read. A photo that may not be a business record raises a review question.
  With the model on this Mac, a photo with no words cannot be described yet.
- **Pages you edit:** with the model on this Mac, the top of a page (Overview, Current
  facts, Related, Documents) is kept up to date by the wiki. Edit it and the wiki leaves
  it as you wrote it; everything below it is never changed.
- **Your rules:** edit `HOUSE-RULES.md` in the wiki folder.

**Update Packets** are short notes Claude writes at the end of a working conversation. To
get them, upload the `skills/packet/` folder (zip it first) to your Claude account as a
skill, say "wrap up" at the end of a conversation, and upload the file it gives you.

## Updating and switching

- **Update:** double-click **Update Wiki Engine** in the wiki folder. Your pages,
  documents, house rules and settings are never touched. To undo an update, run
  `git revert HEAD` in the wiki folder.
- **Update between runs:** the update does not start while the wiki is reading files.
  Wait until the Upload page shows **Idle** (or only "file(s) waiting to start"); a
  large upload can take hours.
- **Switch between Claude and This Mac only:** paste the install line again and choose
  the other option. Your wiki stays as it is.

## If something looks stuck

| You see | Do this |
|---|---|
| Files stay "Queued" | Open System Settings → General → Login Items & Extensions → *Allow in the Background*, turn **bash** on, then press **Process now**. |
| Files say "Waiting to convert" for a long time | Normal for a large upload: they are in a later batch. The live line shows which batch is being read. |
| "Nothing has moved for …" on the Upload page | Press **Restart processing** there. What is filed stays filed; the rest is read again. |
| Update says "The wiki is processing files right now" | Wait until the Upload page shows **Idle**, then update again. If it already showed Idle, try again in 15 minutes. |
| The Upload page says "Forbidden" | Reload the page. The wiki restarted, which an update does. |
| "Claude could not run: your Anthropic API credit has run out" | Add credit at console.anthropic.com (Billing), then press **Process now**. |
| "Claude could not run: … usage limit …", "Anthropic's service is having trouble", "this Mac seems to be offline" | Nothing to do. The documents wait, and the next run tries again. |
| "Claude could not run: it is not signed in" | Paste the install line again and replace the sign-in. |
| "The local model could not run" | Paste the install line again. It checks and repairs the model. |
| "The model on this Mac stopped responding" | Nothing to do: the rest is read on the next run. A file it stops on twice is set aside in `raw/_needs-review/`. |
| A file says **Needs review** | It could not be read twice. Click the label: its question in the Review tab offers to read it again (add a note saying what it is), set it aside, or leave it. |
| No notifications appear | Open System Settings → Notifications → **Script Editor** and allow notifications. |
| macOS asks to let "Python" find devices on local networks | Choose **Don't Allow**. The wiki only talks to this Mac and works either way. If you allowed it earlier, switch it off in System Settings → Privacy & Security → Local Network. |

Logs are in `~/Library/Logs/wiki-starter/`. Costs, privacy and how the two reading
options compare are in `INSTALL.md`.

## What stays where

- Your documents and the wiki: in `~/Wiki/<name>` on this Mac. Back it up with Time Machine.
- The site: served only to this Mac. Nothing on your network can open it, and the wiki
  never looks for or connects to other devices on it.
- Sent to Anthropic: the text Claude reads while it works, and the questions you ask with
  the pages that answer them, nothing else. With the model on
  this Mac, nothing at all.
- Sent to TypeSafe, only if you save a TypeSafe key (Claude only): each review question,
  its answers, and the parts of the wiki and the document it is about; and, only if you
  turn on Jev's overview check (`"overviews": {"jev": true}` in `wiki.config.json`), a
  page's overview with the facts it was written from; and, only if you turn on Jev's choice of
  reader (`"fast": {"routing": {"jev": true}}`), the start of each document code cannot
  place itself (up to 12,000 characters) with its file name. The key is kept in
  the Keychain.
- Version history: a local git history in the wiki folder, never pushed anywhere.

## Uninstall

First copy `~/Wiki/<your wiki>/raw/` somewhere safe if you need the documents. Files you
dragged in with Finder were moved there, so it may hold the only copies.

Then paste the blocks below into Terminal one at a time.

**1. Stop and remove the background processes:**

```sh
for p in ~/Library/LaunchAgents/local.wiki-starter.*.plist; do
  launchctl bootout "gui/$(id -u)" "$p" 2>/dev/null; rm -f "$p"
done
```

**2. Remove the Anthropic sign-in and the TypeSafe key from the Keychain** (they are there
only if Claude was used):

```sh
security delete-generic-password -s wiki-starter -a claude-oauth-token 2>/dev/null
security delete-generic-password -s wiki-starter -a anthropic-api-key 2>/dev/null
security delete-generic-password -s wiki-starter -a typesafe-api-key 2>/dev/null
```

**3. Remove the Desktop shortcuts, the logs and the model on this Mac** (if it was
downloaded):

```sh
rm -f ~/Desktop/"Wiki Inbox" ~/Desktop/"Wiki Intake" ~/Desktop/"Open Wiki.webloc"
rm -rf ~/Library/Logs/wiki-starter ~/Library/"Application Support"/wiki-starter
```

**4. Delete the wiki: its pages, history and documents.** If it is the only wiki on this
Mac, delete the whole Wiki folder:

```sh
rm -rf ~/Wiki
```

To delete only one of several wikis, list them with `ls ~/Wiki` and delete that one by its
folder name, for example `rm -rf ~/Wiki/my-company`.

**Check that nothing is left.** This prints nothing, apart from a "No such file or
directory" line for `~/Wiki` if you deleted it:

```sh
ls ~/Library/LaunchAgents | grep wiki-starter; ls ~/Wiki
```

**Optional:** remove the tools the installer added. Only do this if nothing else on the
Mac uses them:

```sh
brew uninstall llama.cpp 2>/dev/null
brew uninstall ocrmypdf python@3.12 node
rm -rf ~/.local/bin/claude ~/.local/share/claude
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh)"
```

If you remove Homebrew, also delete the `brew shellenv` line from `~/.zprofile`.

## Licence

The site generator is Quartz (MIT, see `LICENSE.txt`).
