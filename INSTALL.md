# Installing the wiki

## Before you start

- A Mac with **macOS 13 (Ventura) or newer** and an **administrator** account. A Mac mini
  that stays on and logged in is ideal, because the wiki updates in the background.
- About **3 GB** of free disk space for the tools, plus room for your documents.
- An **Anthropic** account, either:
  - a **Claude Pro or Max subscription**, or
  - an **API key** from [console.anthropic.com](https://console.anthropic.com/settings/keys).
    Set a monthly spend limit there.

No GitHub account, no website and no other service is needed.

## Install

Open **Terminal** (Applications → Utilities) and paste:

```sh
curl -fsSL https://raw.githubusercontent.com/arkceo/wiki-starter/main/install.sh | bash
```

Paste the line rather than downloading the file: macOS blocks downloaded scripts from
opening with a double-click.

The installer works through seven steps and prints a tick for each. The first run takes
10–20 minutes, mostly downloading tools. It asks for:

1. **Your Mac password**, once, to install [Homebrew](https://brew.sh). Skipped if
   Homebrew is already installed.
2. **Company name** and **wiki title**. The wiki goes in `~/Wiki/<title>`.
3. **Anthropic sign-in.**
   - *Subscription:* a browser window opens. Sign in, approve, and copy the token that
     Terminal shows (it starts with `sk-ant-oat`). Paste it when asked.
   - *API key:* paste the key when asked.
   Either way it is stored in your **macOS Keychain**, not in a file.

If anything fails, run the same line again. Finished steps are skipped.

## What gets installed

| Where | What |
|---|---|
| Homebrew | `git`, `node`, `python@3.12`, `ocrmypdf` (with Tesseract and Ghostscript) |
| `~/.local/bin/claude` | Claude Code |
| `~/Wiki/<title>/` | the wiki: engine, your pages, your documents, local history |
| `~/Library/LaunchAgents/local.wiki-starter.<title>.*.plist` | the runner and the viewer |
| `~/Library/Logs/wiki-starter/` | logs (no secrets) |
| Desktop | **Wiki Inbox**, **Wiki Intake** (links into the wiki folder), **Open Wiki** |
| Keychain | your Anthropic sign-in, under "wiki-starter" |

## Using it

- **Documents** → drop into **Wiki Intake**. Whole folders work too.
- **Update Packets** → drop into **Wiki Inbox**. To get them, upload the `skills/packet/`
  folder (zip it first) to your Claude account in its Skills settings,
  then say "wrap up" at the end of a conversation.
- **Read** → double-click **Open Wiki**, or visit `http://127.0.0.1:8765`.
- **Ask** → open the wiki folder in the Claude desktop app, or run `claude` in Terminal
  inside it, and ask. Claude answers from the wiki with citations.
- **Your rules** → edit `HOUSE-RULES.md` in the wiki folder: who you are, how to file
  things, what never to record. Claude follows it on every run.
- **Process Now** (in the wiki folder) runs the runner immediately.
- **Review queue** → the *Review queue* page lists anything Claude was unsure about.

## Costs

- **Subscription:** processing counts against your plan's usage limits.
- **API key:** you pay per use. A typical document costs cents; a long scanned contract
  can cost more. Each run is capped (default US$5, `maxSpendPerRunUsd` in
  `wiki.config.json`), and a file that fails twice is set aside instead of retried.
- Everything else is free.

## Privacy

- Documents and the wiki stay on this Mac, in `~/Wiki/<title>`.
- The site is served only to this Mac (`127.0.0.1`). Other devices cannot open it.
- The text Claude reads while working is sent to Anthropic to be processed.
- Source documents are not kept in the wiki's git history. **Turn on Time Machine.**

## Updating the engine

Double-click **Update Wiki Engine** in the wiki folder. It downloads the newest engine and
replaces engine files only: scripts, site code, prompts, `CLAUDE.md`. It never touches
`wiki/`, `raw/`, `archive/`, your projects under `generated/`, `index.md`, `log.md`,
`HOUSE-RULES.md` or `wiki.config.json`. An engine file you edited is backed up to
`.wiki-engine/backup/` first. Undo an update with `git revert HEAD` in the wiki folder.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Nothing seems to happen after dropping a file | It is probably working: a batch of PDFs takes 5–15 minutes. Watch it with `tail -f ~/Library/Logs/wiki-starter/runner.log` (Ctrl+C stops watching, not the work). **Process Now** says so if a run is already busy. If no "Wiki is working" notice appeared, allow notifications for *Script Editor* in System Settings → Notifications. |
| "Wiki needs attention: Claude could not run" | The sign-in expired or was revoked. Run the install line again and choose to replace the sign-in. |
| A file was moved to `raw/_needs-review/` | It failed twice. Check it opens, then drop it into Wiki Intake again. The Review queue says why. |
| Scanned PDF summarised badly | OCR quality depends on the scan. The original is always kept and linked from the page. |
| Open Wiki shows "being built" | The first build takes a minute or two. Refresh. |
| The wiki stopped updating after a restart | Log in: background agents only run while a user is signed in. Consider automatic login on a dedicated Mac mini. |
| Anything else | `~/Library/Logs/wiki-starter/runner.log` and `install.log` |

## Removing it

```sh
for a in ~/Library/LaunchAgents/local.wiki-starter.*.plist; do
  launchctl bootout "gui/$(id -u)" "$a"; rm "$a"
done
security delete-generic-password -s wiki-starter -a claude-oauth-token 2>/dev/null
security delete-generic-password -s wiki-starter -a anthropic-api-key 2>/dev/null
rm ~/Desktop/"Wiki Inbox" ~/Desktop/"Wiki Intake" ~/Desktop/"Open Wiki.webloc"
```

This stops the wiki and removes the sign-in. Your wiki folder in `~/Wiki/` is left in
place; delete it yourself if you no longer need it.
