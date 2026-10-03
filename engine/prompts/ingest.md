Read CLAUDE.md (it imports HOUSE-RULES.md).

You are running unattended. Ingest ONLY the Update Packets in raw/inbox/ — the *.md
files. Process AT MOST FIVE packets this run: the oldest five by file name. Leave the rest
untouched; the runner calls you again until the inbox is empty. Ignore .gitkeep, any file
whose name starts with "Icon", and any non-Markdown file.

Do not re-read the filed sources under raw/<project>/; they are already synthesised.
Consult one only if a packet explicitly requires it, and then through its
cache/md/<path>.md mirror if one exists.

For each packet:
1. Apply what it decided, changed or learned: update wiki/, index.md and log.md.
2. Regenerate any affected generated/ export.
3. Record contradictions or uncertainty in wiki/_review.md instead of overwriting.
4. Move the processed packet from raw/inbox/ to archive/inbox/ with a plain shell `mv`.
   If a packet is already reflected in the wiki, make no edits but still move it.
5. If a file contains git conflict markers (<<<<<<<, =======, >>>>>>>), treat each side
   as a separate packet.
6. A packet whose Details carry `Review item: RV-...` is the answer to a review question,
   given by the owner or by the auto-review. Apply that answer to the pages it names, add a
   line under the matching wiki/_review.md entry saying it was resolved, with the answer
   and the date, and never raise the same question again. If the answer sets a document
   aside, move the original into raw/_set-aside/ with a plain shell `mv`, keep its page
   but mark it `status: superseded` with a line saying the owner set it aside, and take
   it out of index.md.

Suggested actions. When a packet is a review answer (its Details carry `Review item:
RV-...`) and the answer means someone must do something outside the wiki (file or correct
a return, pay or chase an amount, renew a licence, sign or send a document, ask a party
for something), also write ONE suggested action, so the owner can hand the work to an AI
agent, an employee or an outside agent: a JSON file
actions/open/AC-<YYYYMMDD>-<short-slug>.json, with a name not already used in
actions/open/ or actions/done/. Exactly these keys:

{
  "id": "AC-<YYYYMMDD>-<short-slug>",
  "created": "<YYYY-MM-DD>",
  "from": "<the review item's RV-... id>",
  "title": "<what must be done, one line, at most 160 characters>",
  "why": "<what the wiki found, and what goes wrong if nothing is done>",
  "due": "<YYYY-MM-DD, only when a document or the wiki gives the deadline; otherwise empty>",
  "priority": "high" or "normal" or "low",
  "steps": ["<each step, in order; at most 12>"],
  "briefs": {
    "ai_agent": "<the brief for an AI agent with a browser>",
    "employee": "<the brief for a member of staff>",
    "external": "<the brief for an outside professional>"
  },
  "assignee": "",
  "sources": ["<each document and wiki page the facts come from, by path>"],
  "status": "open"
}

Each brief stands on its own: whoever gets it sees nothing else. Say what is to be done
and why; the facts and figures from the wiki and the documents (names, numbers, periods,
amounts, references), each with its path; the steps; what to hand back or report; and the
deadline. The ai_agent brief tells an AI agent with a browser what to open, check and
prepare, and that it must stop and ask the owner before it submits anything official or
pays anything. The employee brief is for a member of staff. The external brief is formal,
for an outside professional (a tax agent, the company secretary, a lawyer). Never invent a
fact, a figure or a date: leave "due" empty when no date is known, and say in the briefs
what is not known. Write no action when nothing needs doing outside the wiki, and never
change an action that already exists (when an answer is changed, the engine itself dismisses
the actions drafted from the earlier answer).

In every frontmatter value you write, wrap the value in double quotes if it contains a
colon followed by a space or a " #".

How to use your tools: write and edit pages only with the Write and Edit tools. Use the
shell for exactly two things, `mkdir -p <folder>` and `mv <file> <destination>`, one
command per call. Never chain commands with &&, ; or |, and never write a file through
the shell; anything else is refused.

Edit and move files only. Do not run git, do not use the network, do not touch anything
outside this folder, and never edit an engine file. Never stop to ask.
