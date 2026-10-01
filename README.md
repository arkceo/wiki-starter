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
  - **This Mac only**: no account is needed, but the Mac must have Apple silicon and
    **16 GB of memory**.
- About 3 GB of free disk space (about 9 GB with the model on this Mac), and an internet
  connection.

**Already installed?** You don't need to reinstall. Double-click **Update Wiki Engine** in
the wiki folder. For a clean start instead, uninstall first (last section).

## Install

Open **Terminal** (Applications → Utilities), paste this line and press Return:

```sh
curl -fsSL https://raw.githubusercontent.com/arkceo/wiki-starter/main/install.sh | bash
```

The installer has 7 steps, takes 10–20 minutes the first time, and shows ✓ as each step
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

It finishes by processing a welcome note as a test and opening the wiki in your browser.
If macOS shows **Background Items Added**, leave it allowed.

**If any step fails, paste the same line again.** Finished steps are skipped.

## Daily use

- **Read the wiki:** double-click **Open Wiki** on the Desktop, or go to
  `http://127.0.0.1:8765`.
- **Add documents or notes:** click **Upload** at the top right of any wiki page. Drop in
  files, whole folders or Update Packets, or use **Choose files**. Your originals stay
  where they are.
- **Watch progress:** the Upload page shows each file moving through its real stages
  (upload, convert, read and write, file), an "n of m done" count, a live line saying what
  the wiki is doing at that moment, and a log of what it has done. You get a notification
  when the wiki has been updated.
- **Process immediately:** press **Process now** on the Upload page.
- **Ask questions** (Claude only): open the wiki folder in the Claude desktop app, or run
  `claude` in Terminal inside that folder.
- **Review queue:** the *Review queue* page lists anything the wiki was unsure about. The
  model on this Mac only catches obvious contradictions.
- **Your rules:** edit `HOUSE-RULES.md` in the wiki folder.

**Update Packets** are short notes Claude writes at the end of a working conversation. To
get them, upload the `skills/packet/` folder (zip it first) to your Claude account as a
skill, say "wrap up" at the end of a conversation, and upload the file it gives you.

## Updating and switching

- **Update:** double-click **Update Wiki Engine** in the wiki folder. Your pages,
  documents, house rules and settings are never touched. To undo an update, run
  `git revert HEAD` in the wiki folder.
- **Switch between Claude and This Mac only:** paste the install line again and choose
  the other option. Your wiki stays as it is.

## If something looks stuck

| You see | Do this |
|---|---|
| Files stay "Queued" | Open System Settings → General → Login Items & Extensions → *Allow in the Background*, turn **bash** on, then press **Process now**. |
| The Upload page says "Forbidden" | Reload the page. The wiki restarted, which an update does. |
| "Claude could not run" | Paste the install line again and replace the sign-in. |
| "The local model could not run" | Paste the install line again. It checks and repairs the model. |
| A file was moved to `raw/_needs-review/` | It failed twice. Check that it opens, then upload it again. |
| No notifications appear | Open System Settings → Notifications → **Script Editor** and allow notifications. |
| macOS asks to let "Python" find devices on local networks | Choose **Don't Allow**. The wiki only talks to this Mac and works either way. |

Logs are in `~/Library/Logs/wiki-starter/`. Costs, privacy and how the two reading
options compare are in `INSTALL.md`.

## What stays where

- Your documents and the wiki: in `~/Wiki/<name>` on this Mac. Back it up with Time Machine.
- The site: served only to this Mac. Nothing on your network can open it, and the wiki
  never looks for or connects to other devices on it.
- Sent to Anthropic: the text Claude reads while it works, nothing else. With the model on
  this Mac, nothing at all.
- Version history: a local git history in the wiki folder, never pushed anywhere.

## Uninstall

First copy `~/Wiki/<your wiki>/raw/` somewhere safe if you need the documents. Files you
dragged in with Finder were moved there, so it may hold the only copies.

Then paste the following into Terminal. In the last line, use your own wiki's folder name:

```bash
# 1. Stop and remove the background processes
for p in ~/Library/LaunchAgents/local.wiki-starter.*.plist; do
  launchctl bootout "gui/$(id -u)" "$p" 2>/dev/null; rm -f "$p"
done

# 2. Remove the Anthropic sign-in (if Claude was used)
security delete-generic-password -s wiki-starter -a claude-oauth-token 2>/dev/null
security delete-generic-password -s wiki-starter -a anthropic-api-key 2>/dev/null

# 3. Remove Desktop shortcuts, logs and the local model (if downloaded)
rm -f ~/Desktop/"Wiki Inbox" ~/Desktop/"Wiki Intake" ~/Desktop/"Open Wiki.webloc"
rm -rf ~/Library/Logs/wiki-starter
rm -rf ~/Library/"Application Support"/wiki-starter

# 4. Delete the wiki: pages, history and documents
rm -rf ~/Wiki/company-wiki
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
