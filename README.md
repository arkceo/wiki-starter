# wiki-starter

A company wiki that writes itself and lives only on your Mac.

Click **Upload**, drop in documents and notes, and watch the progress live. Claude reads
them, writes and cross-links the wiki pages, and files the originals. You read the result
in your browser.
Nothing is published. The only account you need is Anthropic's, and none at all if you
choose to have the documents read by a model that runs on the Mac itself.

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
3. who should read your documents:
   - **Claude** (recommended): the richest wiki. Needs an Anthropic sign-in, your Claude
     subscription (Pro or Max) or an API key.
   - **This Mac only**: an open model ([Qwen](https://huggingface.co/bartowski/Qwen_Qwen3.5-9B-GGUF))
     runs on the Mac. Nothing leaves it and no account is needed; the wiki is plainer.
     Needs 16 GB of memory and a one-off download of about 6.2 GB.

That's it. The installer puts **Open Wiki** on your Desktop and opens the wiki.
Full details, costs and troubleshooting: [INSTALL.md](INSTALL.md).

## Daily use

| Do this | And this happens |
|---|---|
| Click **Upload** (top right) and drop PDFs, Word, Excel, PowerPoint, scans or photos | Each is converted to text (OCR for scans), summarised into the right pages, and filed. The page shows every file's progress and a live log |
| Upload an Update Packet (`.md`) the same way | Its decisions and facts are applied to the affected pages |
| Double-click **Open Wiki** | The wiki opens at `http://127.0.0.1:8765` |
| Open the wiki folder in Claude and ask a question | Claude answers from the wiki, with citations |

A notification tells you when the wiki has been updated.

**Update Packets** are short notes Claude writes at the end of a working conversation.
Upload `skills/packet/` to your Claude account as a skill, say "wrap up" at the end of a
conversation, and upload the file it gives you.

## What stays where

- Your documents and the wiki: in `~/Wiki/<name>` on this Mac. Back it up with Time Machine.
- The site: served only to this Mac. Nothing on your network can open it.
- Sent to Anthropic: the text Claude reads while it works, nothing else. With the model on
  this Mac, nothing at all.
- Version history: a local git history in the wiki folder, never pushed anywhere.

## Updating

Double-click **Update Wiki Engine** in the wiki folder. It replaces the engine (scripts,
site code, prompts) and never touches your pages, documents or `HOUSE-RULES.md`.

## Licence

The site generator is [Quartz](https://quartz.jzhao.xyz/) (MIT, see `LICENSE.txt`).
