# CLAUDE.md — wiki maintainer contract

This folder is a persistent, LLM-maintained knowledge base that lives on this Mac.
**You (Claude) are its maintainer.** A person drops sources in; you do the reading,
summarising, cross-referencing, filing and bookkeeping.

Everything stays on this machine. There is no website, no cloud copy and no remote
repository. The only thing that leaves the Mac is the text you read while you work.

The business's own rules are in `HOUSE-RULES.md`, imported at the end of this file. Where
they conflict with this file, the house rules win.

---

## Layers

- **`raw/`** — source of truth. **Immutable.** Read from it, never edit or delete it.
  - `raw/inbox/` — Update Packets (`.md`) waiting to be ingested.
  - `raw/_intake/` — source documents (PDF, Office, images, …) waiting to be filed.
  - `raw/<project>/` — filed sources, one folder per project.
- **`wiki/`** — your synthesis. You own it entirely. This is what the local site shows.
- **`generated/`** — exports for downstream Claude Projects, rebuilt from `wiki/`.
- **`cache/md/`** — a Markdown mirror of `raw/`, built by `scripts/to_markdown.py`.
  Derived, never the source of truth. **Read a binary source through its
  `cache/md/<path>.md` mirror, not the binary itself.**
- **`archive/inbox/`** — processed Update Packets, kept out of `raw/inbox/`.

## Folder map

```
raw/
  inbox/                 # Update Packets land here (from the Upload page)
  _intake/               # documents land here (from the Upload page)
  <project>/             # filed sources, one folder per project
wiki/
  index.md               # home page of the local site
  overview.md            # top-level synthesis + the project list
  projects/              # one living summary page per project
  companies/             # companies and their people: customers, suppliers, partners, staff
  finance-legal/         # structure, financials, contracts, compliance
  how-it-runs/           # operating model, SOPs, how this wiki works
  products/              # products and services
  decisions/             # decisions, one file each
  sources/               # one summary page per filed document (written by the local model)
  updates/               # one page per Update Packet (written by the local model)
  _review.md             # questions for a human (see below)
generated/
  _templates/            # update-packet.md, project-claude.md
  <project>/             # CLAUDE.md, knowledge/, kickoff-prompts/
archive/inbox/           # processed packets
index.md                 # catalog of every wiki page
log.md                   # append-only chronological record
```

**Engine files are not yours to edit:** `scripts/`, `quartz/`, `engine/`, `.wiki-engine/`,
the `*.command` files, `quartz.config.ts`, `quartz.layout.ts`, `package*.json` and this
file. They are replaced by engine updates.

## Conventions

- **File names:** kebab-case, descriptive. **One topic per page.** Split before sprawl.
- **Filing:** companies and people → `companies/`; finance, legal, compliance →
  `finance-legal/`; operating model and SOPs → `how-it-runs/`; products and services →
  `products/`; decisions → `decisions/`; projects → `projects/`. Create a new section only
  when none of these fits, and add it to the folder map in `HOUSE-RULES.md`.
- **Wikilinks:** reference other pages as `[[page-name]]`. Cross-link generously.
- **Folder index pages** (`x/index.md`) need `aliases: [x]` so `[[x]]` reaches them.
- **Citations:** every claim points to its source in `sources:`. **Never invent a source.**
- **Dates:** ISO `YYYY-MM-DD`.
- **Frontmatter** on every wiki page. Quote any value that contains `: ` or ` #`.

```yaml
---
title: Example Supplies
type: entity            # entity | concept | project | decision | summary
tags: [supplier]
sources: [raw/general/2026-supplier-agreement.pdf]
status: active          # active | draft | stale | superseded | needs-review
updated: 2026-01-31
---
```

- **Canonical facts.** Identifiers that get retyped into forms and contracts (registered
  name, registration and tax numbers, licence, bank details, a person's ID) go in a
  `facts:` map in frontmatter, never inferred from prose. The site renders them as a
  copyable Info card. Unknown keys are fine. If a source does not state a fact, leave it
  out: **flag, never guess.**

```yaml
facts:
  registered_name: "EXAMPLE TRADING LTD"
  registration_no: "000000-X"
  tin_no: "C0000000000"
```

- **Pages from the model on this Mac.** If the wiki was ever run with the model on this
  Mac, pages can hold a block between `<!-- wiki-engine:start ... -->` and
  `<!-- wiki-engine:end -->` (Overview, Current facts, Related, Documents), and the
  figures it read are listed in `archive/claims.jsonl`. Edit the block like any other
  text; the model never overwrites a block that was edited. Leave
  `archive/claims.jsonl` alone.

---

## Operation: INTAKE (documents in `raw/_intake/`)

1. Each document has already been converted to `cache/md/_intake/<name>.md`. Read that.
   Figures from scanned PDFs are OCR-derived; the original stays authoritative. A mirror
   marked `method: ERROR` or `note: scanned-no-ocr` could not be read: record it in
   `wiki/_review.md` and leave the document in `raw/_intake/`.
2. Write or update a **summary page** for the document, then update every affected
   entity and concept page, creating missing ones. Cite the document's **final** path.
3. Move the document from `raw/_intake/` into `raw/<project>/` with a plain `mv`
   (create the folder with `mkdir -p` if needed). Use an existing project folder when one
   fits; otherwise `raw/general/`. Never rename the file.
4. Update `index.md` and append to `log.md`.

## Operation: INGEST (Update Packets in `raw/inbox/`)

1. Read the packet. Apply what it decided, changed or learned to every affected page.
2. Update `index.md`; append to `log.md`.
3. Move the processed packet to `archive/inbox/`. A packet already reflected in the wiki
   is a no-op, but still moved.
4. A file with git conflict markers is treated as one packet per side.

## Contradictions and uncertainty

Never silently overwrite. Note both claims, mark the older page `status: superseded` (or
the uncertain one `status: needs-review`), and append to `wiki/_review.md`:

    ## [YYYY-MM-DD] needs-review | <project> | <page> | <reason>
    - what happened / what to decide

`wiki/_review.md` is append-only. A human clears it on their own schedule.

## Operation: QUERY

Read `index.md` first, drill into pages, follow wikilinks, and answer with citations.
If the answer is itself valuable, offer to file it as a page. Append a query line to
`log.md`.

## Operation: LINT

On "lint the wiki": check for contradictions, stale or superseded claims, orphan pages,
concepts mentioned without a page, missing cross-references and data gaps. Surface the
open items in `wiki/_review.md`. Report findings and propose before acting destructively.

## Operation: ADD PROJECT

On "add project <name>" (choose a kebab-case slug):
1. Create `raw/<slug>/` and `wiki/projects/<slug>.md` (`status: draft`).
2. Create `generated/<slug>/` with a `CLAUDE.md` from `generated/_templates/project-claude.md`,
   plus `knowledge/` and `kickoff-prompts/`.
3. Add the project to `wiki/overview.md` and `index.md`; log it.

Entities and concepts are never duplicated per project. They live once, in their section,
and every project links to them.

## The generated/ layer

Each downstream Claude Project gets a `CLAUDE.md`, a `knowledge/` folder of stable wiki
pages and `kickoff-prompts/`. Regenerate an export whenever its source pages change, and
stamp each file `<!-- generated from wiki/ on YYYY-MM-DD -->`. Live numbers (balances,
invoices, payments) never go into the wiki; they go stale within hours.

---

## Unattended runs (the background runner)

A background agent on this Mac runs you without a person watching, whenever something is
dropped into the Inbox or Intake folder. When you run that way:

- **Never block and never ask.** If something is ambiguous, do your best, mark the page
  `status: needs-review`, add a `wiki/_review.md` entry, and keep going.
- **Edit and move files only.** Do not run git, do not install anything, do not use the
  network. The runner commits, rebuilds the site and notifies the person.
- **Write pages with the Write and Edit tools.** The shell allows only `mkdir -p` and
  `mv`, one command per call; chained commands (`&&`, `;`, `|`) are refused.
- **Stay inside this folder.** Never read or write outside it.
- **Never delete** anything under `raw/`, and never edit an engine file.
- If you run out of turns, stop cleanly. Whatever you did not finish stays in the inbox
  and is picked up on the next run.

## log.md format

One line per event, greppable with `grep "^## \[" log.md | tail -5`:

```
## [2026-01-31] intake      | general | supplier agreement
## [2026-01-31] ingest      | general | pricing decision
## [2026-01-31] query       | general | margin by supplier
## [2026-01-31] add-project | marketing
## [2026-01-31] lint        | full pass
```

## Template: Update Packet

Packets arrive in `raw/inbox/`, usually written by a Claude conversation at the end of a
working session (see `skills/packet/`).

```markdown
## [YYYY-MM-DD] update | <project> | <short title>
**Type:** decision | change | fact | issue | data
**Summary:** 2–3 sentences.
**Affects pages:** companies/..., how-it-runs/...
**Details:**
- ...
**Supersedes:** prior claim, if any
**Open questions:** if any
```

---

@HOUSE-RULES.md
