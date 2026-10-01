Read CLAUDE.md (it imports HOUSE-RULES.md) and wiki/how-it-runs/raw-to-markdown-conversion.md.

You are running unattended. Process the SOURCE documents waiting in raw/_intake/,
including any in subfolders. Ignore README.md, .gitkeep, .DS_Store, any file whose name
starts with "Icon" or "~$", and unfinished downloads (*.download, *.crdownload, *.part,
*.partial): leave those exactly where they are.

Each document has already been converted to Markdown at cache/md/_intake/<file name>.md.
Read THAT mirror, never the raw binary. Figures from scanned PDFs are OCR-derived; the
original document stays authoritative. If a mirror is missing, says `method: ERROR`, or
says `note: scanned-no-ocr`, do not guess at its content: add a wiki/_review.md entry
naming the file and leave the document where it is.

For each readable document:
1. Decide which project it belongs to (an existing raw/<project>/ folder, or
   raw/general/ when none fits) and what its final path will be.
2. Summarise it into the right wiki page(s) per the filing rules in CLAUDE.md, creating
   or updating the relevant entity and concept pages with wikilinks. Cite the document by
   its FINAL path in `sources:`. Put recyclable identifiers in a `facts:` map; never guess
   one.
3. Record any contradiction or uncertainty in wiki/_review.md instead of overwriting.
4. Move the original document from raw/_intake/ into its project folder with a plain
   shell `mv` (use `mkdir -p` first if the folder is new). Never rename it.

Then update index.md and append to log.md, and regenerate any affected generated/ export.

In every frontmatter value you write, wrap the value in double quotes if it contains a
colon followed by a space or a " #".

How to use your tools: write and edit pages only with the Write and Edit tools. Use the
shell for exactly two things, `mkdir -p <folder>` and `mv <file> <destination>`, one
command per call. Never chain commands with &&, ; or |, and never write a file through
the shell; anything else is refused.

Edit and move files only. Do not run git, do not use the network, do not touch anything
outside this folder, and never edit an engine file. Never stop to ask.
