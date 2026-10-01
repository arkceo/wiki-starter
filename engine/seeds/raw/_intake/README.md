# The document queue

Use **Upload** at the top right of the wiki instead of this folder: it shows each file's
progress and a live log.

This folder is where uploaded documents wait to be processed. Anything copied here in
Finder is picked up too: the background runner waits until it has finished copying,
converts it to text (with OCR for scans), has Claude summarise it into the right wiki
pages, and files the original under `raw/<project>/`.

- Update Packets (`.md` notes from a Claude conversation) belong in `raw/inbox/`. A packet
  put here is moved there automatically.
- Files still downloading (`.download`, `.part`, `.crdownload`) are left alone until they
  finish.
- Nothing here is published. Everything stays on this Mac; only the text Claude reads is
  sent to Anthropic for processing.
