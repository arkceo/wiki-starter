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
review/
  open/                  # questions the owner can answer with one click (Upload page, Review tab)
  done/                  # answered questions, with the answer and who gave it
actions/
  open/                  # suggested actions: work someone must do outside the wiki (Upload page)
  done/                  # actions done or dismissed
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

- **Employees' pages** keep the person's full details as the documents give them: the IC
  or passport, EPF, SOCSO and tax numbers and the bank account in `facts:` (`id_no`,
  `epf_no`, `socso_no`, `tin_no`, `bank_account`); date of birth, address, phone, email
  and salary on the page. A number goes on a person's card only when the document prints
  it as theirs, under its label: an employer's EPF or SOCSO number in a payslip's header,
  a contribution or a year is not. Leave a field empty rather than guess.

- **Pages written by the wiki engine.** If the wiki was ever run with the model on this
  Mac, or with Claude's fast reading (the default with Claude), pages can hold a block between
  `<!-- wiki-engine:start ... -->` and `<!-- wiki-engine:end -->` (Overview, Current
  facts, Related, Documents), and the figures it read are listed in
  `archive/claims.jsonl`. Edit the block like any other text; the engine never overwrites
  a block that was edited. Leave `archive/claims.jsonl` alone.

## Menu

The local site's left menu shows the wiki in sections (Company Profile, Marketing, Sales,
Customer Service, Human Resources, Operation, Legal, Account, Finance), each with its
categories. Pages never move for it: a page appears under every category its frontmatter
lists in `menu:`, as `section/category` keys.

```yaml
menu: ["company-profile/accountant", "company-profile/tax-agent"]
```

- **Summary and topic pages:** the one or two categories that fit best, most fitting first.
- **Company and people pages:** every category that applies. Parties go under Company
  Profile by what they are to the business (a firm that is both its accountant and its
  tax agent gets both). The business's staff go under Employees; its directors and
  shareholders, and the business's own page, under The company itself. A person at
  another company goes where that company is.
- **Government bodies:** one page each, whatever name a document uses: titled with the
  body's name in `engine/menu.json`, with its other names from there in `aliases:`
  (LHDN, Lembaga Hasil Dalam Negeri and Inland Revenue Board of Malaysia are one page),
  and under `company-profile/government` only. A business whose name only holds a word
  of a body's name (Hasil Laut Segar, Bomba Safety Services) is a business, with its own
  page.
- **Never** on `index.md`, `overview.md`, folder index pages, or project, decision,
  update and product pages: they keep their own groups in the menu.
- The engine adds the categories it can tell and never removes one. `menu_auto: false`
  in a page's frontmatter keeps it away from that page's menu.

The keys (`engine/menu.json` describes each):

- **Company Profile:** `company-profile/company` The company itself;
  `company-profile/employees` Employees; `company-profile/contractors` Contractors;
  `company-profile/customers` Customers; `company-profile/suppliers` Suppliers;
  `company-profile/company-secretary` Company Secretary; `company-profile/accountant`
  Accountant; `company-profile/auditor` Auditor; `company-profile/tax-agent` Tax Agent;
  `company-profile/lawyer` Lawyer; `company-profile/government` Government bodies;
  `company-profile/banks` Banks & Financiers; `company-profile/landlords` Landlords;
  `company-profile/insurers` Insurers
- **Marketing:** `marketing/brand` Brand and logo; `marketing/campaigns` Campaigns and
  promotions; `marketing/digital` Digital and social media; `marketing/research` Market
  research; `marketing/events` Events; `marketing/materials` Marketing materials;
  `marketing/agencies` Agencies
- **Sales:** `sales/quotations` Quotations; `sales/orders` Orders and sales contracts;
  `sales/price-lists` Price lists; `sales/tenders` Tenders and proposals; `sales/reports`
  Sales reports; `sales/agents` Agents and commissions
- **Customer Service:** `customer-service/enquiries` Enquiries and complaints;
  `customer-service/returns` Warranties, returns and refunds;
  `customer-service/service-levels` Service levels; `customer-service/feedback` Feedback
  and reviews; `customer-service/faqs` FAQs; `customer-service/after-sales` After-sales
  records
- **Human Resources:** `human-resources/recruitment` Recruitment and onboarding;
  `human-resources/contracts` Employment contracts; `human-resources/payroll` Payroll and
  statutory contributions; `human-resources/leave` Leave and attendance;
  `human-resources/policies` Policies and handbook; `human-resources/training` Training;
  `human-resources/performance` Performance and discipline; `human-resources/permits`
  Foreign worker permits
- **Operation:** `operation/premises` Premises and facilities; `operation/equipment`
  Equipment and assets; `operation/inventory` Inventory; `operation/purchasing`
  Purchasing; `operation/logistics` Logistics and delivery; `operation/sops` Processes and
  SOPs; `operation/it` IT and systems; `operation/safety` Health and safety
- **Legal:** `legal/secretarial` Company secretarial; `legal/contracts` Contracts
  register; `legal/licences` Licences and permits; `legal/ip` Intellectual property;
  `legal/disputes` Disputes; `legal/compliance` Compliance
- **Account:** `account/bookkeeping` Bookkeeping and ledgers; `account/receivables` Sales
  invoices and receivables; `account/payables` Supplier bills and payables;
  `account/reconciliations` Bank reconciliations; `account/tax` Tax filings;
  `account/audit` Audit and financial statements
- **Finance:** `finance/budgets` Budgets and forecasts; `finance/cash-flow` Cash flow;
  `finance/loans` Loans and financing; `finance/facilities` Banking facilities;
  `finance/investments` Investments and fixed assets; `finance/insurance` Insurance;
  `finance/grants` Grants and incentives

---

## Operation: INTAKE (documents in `raw/_intake/`)

1. Each document has already been converted to `cache/md/_intake/<name>.md`. Read that.
   Figures from scans and photos are OCR-derived; the original stays authoritative. Look
   at the original image or PDF too when it is a photo, or when the mirror's text is
   missing, empty or garbled. A document that cannot be read either way: record it in
   `wiki/_review.md` and leave it in `raw/_intake/`.
   A photo is a document even with no words in it: its summary page says what the photo
   shows and when it was taken. Describe people; never name anyone from their face.
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
5. A packet carrying `Review item: RV-...` is an answer to a review question: apply it,
   and note under the matching `wiki/_review.md` entry that it was resolved. If the
   answer means someone must do something outside the wiki (correct a return, chase a
   payment, renew a licence), also write one suggested action in `actions/open/`, with
   briefs for an AI agent, an employee and an outside agent (the format is in
   `engine/prompts/ingest.md`). Never invent a fact or a deadline in it.

## Contradictions and uncertainty

Never silently overwrite. Note both claims, mark the older page `status: superseded` (or
the uncertain one `status: needs-review`), and append to `wiki/_review.md`:

    ## [YYYY-MM-DD] needs-review | <project> | <page> | <reason>
    - what happened / what to decide

When the owner could settle it by choosing an answer, also write a review item in
`review/open/` (the format is in `engine/prompts/intake.md`); the Upload page's Review
tab shows it with one-click answers. Cite its id as `RV-...` in backticks, never as a
link. `wiki/_review.md` is append-only. A human clears it on their own schedule.

## Operation: QUERY

Read `index.md` first, drill into pages, follow wikilinks, and answer with citations.
If the answer is itself valuable, offer to file it as a page. Append a query line to
`log.md`.

The owner can also ask from the Upload page's Ask tab. Its packets arrive in `raw/inbox/`
named `ask-*.md`: an **Answer** packet asks for the answer to be filed as its own page
(`type: summary`) citing the pages it came from; a **Correction** packet carries the
owner's own statement of what is right. Ingest both like any other packet.

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
