Read CLAUDE.md (it imports HOUSE-RULES.md) and wiki/how-it-runs/raw-to-markdown-conversion.md.

You are running unattended. Process the SOURCE documents waiting in raw/_intake/,
including any in subfolders. Ignore README.md, .gitkeep, .DS_Store, any file whose name
starts with "Icon" or "~$", and unfinished downloads (*.download, *.crdownload, *.part,
*.partial): leave those exactly where they are.

Each document has already been converted to Markdown at cache/md/_intake/<file name>.md.
Read that mirror first: it is the cheapest way to read a document. Figures from scans and
photos are OCR-derived; the original document stays authoritative.

Look at the original itself as well (the Read tool shows images and PDFs) when:
- it is a photo or image (.jpg, .jpeg, .png, .gif, .webp): OCR misses and garbles the text
  in photos, so always look at the picture. For a .heic or .heif photo, read the JPEG copy
  named on the mirror's `preview:` line (cache/md/<that path>) instead;
- the mirror says `note: scanned-no-ocr`, `image-no-ocr` or `image-little-text`, or its
  text is missing, empty or mostly garbled: read the original PDF or image (a PDF longer
  than 20 pages in page ranges).
Never guess at content you could not read. If neither the mirror nor the original can be
read (the mirror says `method: ERROR` and the original is a format the Read tool cannot
show, or will not open), add a wiki/_review.md entry naming the file and leave the
document where it is.

Photos and images are documents too, with words in them or not: a photo you can see is
never "unreadable". Give each one a summary page like any document, with a section
`## What the photo shows`: a plain, factual description of what is visible (the scene or
place, objects, products, equipment, quantities, condition, any documents or screens in
view, and any text, quoted), and when it was taken, from the mirror's `taken:` line. Only
what you can see; say what is unclear. Describe people ("two people in work clothes
unloading boxes") and never name anyone from their face: a name may come only from text
in the photo (a badge, a caption), another document, or a note from the owner. Link the
page to the companies, products, projects or places the photo clearly concerns. If you
cannot tell whether a photo belongs in a business wiki (it looks personal or taken by
accident), still file it, mark its page `status: needs-review`, and write a review item
(kind "uncertain") asking what it is: the business uses it could plausibly have as the
options, and "Not a business record: set it aside" as the last one.

For each readable document:
1. Decide which project it belongs to (an existing raw/<project>/ folder, or
   raw/general/ when none fits) and what its final path will be.
2. Summarise it into the right wiki page(s) per the filing rules in CLAUDE.md, creating
   or updating the relevant entity and concept pages with wikilinks. Cite the document by
   its FINAL path in `sources:`. Put recyclable identifiers in a `facts:` map; never guess
   one. Give every summary, topic, company and person page you write its `menu:` as
   CLAUDE.md's Menu section says, and a government body one page, named as engine/menu.json
   names it.
3. Record any contradiction or uncertainty in wiki/_review.md instead of overwriting.
4. Move the original document from raw/_intake/ into its project folder with a plain
   shell `mv` (use `mkdir -p` first if the folder is new). Never rename it.

Then update index.md and append to log.md, and regenerate any affected generated/ export.

Review items. Each time you add a wiki/_review.md entry for a contradiction, or for an
uncertain or missing fact the owner could settle, also write one review item, so the owner
can answer it with one click: a JSON file review/open/RV-<YYYYMMDD>-<short-slug>.json, with
a name not already used in review/open/ or review/done/. Exactly these keys:

{
  "id": "RV-<YYYYMMDD>-<short-slug>",
  "created": "<YYYY-MM-DD>",
  "kind": "contradiction" or "uncertain" or "missing",
  "file": "<the document's final path, e.g. raw/general/contract.pdf>",
  "question": "<one plain question the owner can answer>",
  "context": "<what each source says, quoted briefly, each with its path>",
  "pages": ["<each wiki page the answer would change>"],
  "options": [
    {"key": "A", "label": "<a short answer>", "effect": "<what the wiki will say if chosen>"},
    {"key": "B", "label": "<another answer>", "effect": "<what the wiki will say>"}
  ],
  "recommended": "<the key you would choose, or empty>"
}

Give two to four real, different answers, each one the sources support or a careful owner
could choose (for example "both are right: they apply to different things"). Put the item's
id in its wiki/_review.md entry as `RV-...` in backticks, never as a [[link]]. Never change
an item that already exists. A document you leave unread needs no item: the runner writes
one if it is still unread after two tries.

In every frontmatter value you write, wrap the value in double quotes if it contains a
colon followed by a space or a " #".

How to use your tools: write and edit pages only with the Write and Edit tools. Use the
shell for exactly two things, `mkdir -p <folder>` and `mv <file> <destination>`, one
command per call. Never chain commands with &&, ; or |, and never write a file through
the shell; anything else is refused.

Edit and move files only. Do not run git, do not use the network, do not touch anything
outside this folder, and never edit an engine file. Never stop to ask.
