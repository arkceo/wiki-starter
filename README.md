# wiki-starter

A company wiki that writes itself and lives only on your Mac.

Drop documents and notes into two folders on your Desktop. Claude reads them, writes and
cross-links the wiki pages, and files the originals. You read the result in your browser.
Nothing is published, and the only account you need is Anthropic's.

## Install

On the Mac that will hold the wiki (a Mac mini that stays on is ideal), open **Terminal**
and paste:

```sh
curl -fsSL https://raw.githubusercontent.com/arkceo/wiki-starter/main/install.sh | bash
```

Step-by-step guide, from install to uninstall:
[Wiki install guide](https://claude.ai/artifact/EVqBQYt9mZP5v15Bk7T4JE)

You will be asked for:

1. your Mac password, once (to install Homebrew and the document tools);
2. your company name and a title for the wiki;
3. an Anthropic sign-in: your Claude subscription (Pro or Max), or an API key.

That's it. The installer puts three things on your Desktop and opens the wiki.
Full details, costs and troubleshooting: [INSTALL.md](INSTALL.md).

## Daily use

| Do this | And this happens |
|---|---|
| Drop PDFs, Word, Excel, PowerPoint, scans or photos into **Wiki Intake** | Each is converted to text (OCR for scans), summarised into the right pages, and filed |
| Drop an Update Packet (`.md`) into **Wiki Inbox** | Its decisions and facts are applied to the affected pages |
| Double-click **Open Wiki** | The wiki opens at `http://127.0.0.1:8765` |
| Open the wiki folder in Claude and ask a question | Claude answers from the wiki, with citations |

A notification tells you when the wiki has been updated.

**Update Packets** are short notes Claude writes at the end of a working conversation.
Upload `skills/packet/` to your Claude account as a skill, say "wrap up" at the end of a
conversation, and drop the file it gives you into Wiki Inbox.

## What stays where

- Your documents and the wiki: in `~/Wiki/<name>` on this Mac. Back it up with Time Machine.
- The site: served only to this Mac. Nothing on your network can open it.
- Sent to Anthropic: the text Claude reads while it works, nothing else.
- Version history: a local git history in the wiki folder, never pushed anywhere.

## Updating

Double-click **Update Wiki Engine** in the wiki folder. It replaces the engine (scripts,
site code, prompts) and never touches your pages, documents or `HOUSE-RULES.md`.

## Licence

The site generator is [Quartz](https://quartz.jzhao.xyz/) (MIT, see `LICENSE.txt`).
