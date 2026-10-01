---
title: Raw-to-Markdown conversion
type: concept
tags: [how-it-runs, wiki]
sources: []
status: active
updated: 2026-01-01
---

Claude never reads a binary document directly. `scripts/to_markdown.py` first builds a
Markdown mirror of `raw/` under `cache/md/`, one `<name>.md` per source. Reading the mirror
is several times cheaper than reading a PDF as page images, and it is the only way to read
Word, Excel and PowerPoint files at all.

## How each kind of file is converted

| Source | Method |
|---|---|
| PDF with a text layer, Word, Excel, PowerPoint, HTML, CSV, JSON, EPUB | MarkItDown |
| Scanned PDF (no text layer) | `ocrmypdf` (Tesseract) adds a text layer, then MarkItDown |
| Plain text and Markdown | copied through |
| Images | MarkItDown (metadata; no OCR) |

Each mirror starts with frontmatter: the source path, a short hash, its size, the method
and the date.

## When a mirror cannot be trusted

- `method: ERROR` — conversion failed. The note says why.
- `note: scanned-no-ocr` — a scanned PDF and OCR was unavailable.

In both cases Claude records the document in [[_review]] and leaves it in the intake folder
instead of guessing at its content. Figures read through OCR can be wrong; the original
document stays authoritative.

## Running it by hand

```sh
.venv/bin/python scripts/to_markdown.py                  # whole raw/ tree
.venv/bin/python scripts/to_markdown.py --only _intake   # just the intake folder
.venv/bin/python scripts/to_markdown.py --force          # rebuild everything
```

The mirror is derived and disposable: delete `cache/` and it is rebuilt on the next run.
