---
name: packet
description: "Write an end-of-session Update Packet for the local wiki: a short Markdown file recording what this conversation decided, changed or learned, ready to upload to the wiki. Use when the user types '/packet', or says 'end of session', 'wrap up', 'generate the packet', 'record this session', 'save this to the wiki', or otherwise signals that a working session is done and should be captured."
---

# End-of-session Update Packet

Distill what **this conversation** decided, changed, built or learned into one Update
Packet. The person uploads the file with the wiki's **Upload** button on their Mac, and
the wiki's background runner folds it into the right pages.

## 1. Decide whether there is anything to record

Review the conversation. If nothing material happened (no decision, change, fact or
learning worth keeping), say so plainly and **stop**. Never invent a packet.

## 2. Pick the project slug

Use the project this conversation served: a short kebab-case name such as `general`,
`sales`, `finance` or a client name. If the person has named their projects, use one of
those. If it is unclear, use `general`.

## 3. Draft the packet

Fill **every** field from the actual conversation. No placeholders. Use today's date in
ISO `YYYY-MM-DD`.

```markdown
## [YYYY-MM-DD] update | <slug> | <short title>
**Type:** decision | change | fact | issue | data
**Summary:** 2–3 sentences.
**Affects pages:** companies/..., how-it-runs/..., projects/<slug>
**Details:**
- ...
**Supersedes:** prior claim, if any
**Open questions:** if any
```

Keep it factual. Name the wiki pages it affects where you know them.

Two limits always apply:

- **Never put credentials in a packet**: passwords, API keys, tokens, bank PINs, or
  connection strings.
- **Keep other people's personal identifiers out** (ID numbers, home addresses) unless
  the record genuinely needs them.

## 4. Hand it over

Produce the packet as a downloadable file named `update-YYYY-MM-DD-<slug>.md`, and tell
the person, in one sentence, to upload it with **Upload** at the top right of their
wiki. The Upload page shows its progress, and a notification confirms the update.
