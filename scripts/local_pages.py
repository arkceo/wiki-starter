#!/usr/bin/env python3
"""local_pages.py — the text of the pages the local engine writes.

The engine (local_engine.py) decides what goes where and reads and writes the files; this
module turns that into Markdown:

  escaping      every string that came from a document or the model shows as plain text:
                no HTML, no links, no Markdown syntax of its own
  engine block  the part of a page the engine keeps up to date, between two markers:
                    <!-- wiki-engine:start sha=... -->  ...  <!-- wiki-engine:end -->
                The marker holds a hash of the block. A block a person has edited no longer
                matches it and is never overwritten; everything outside the block is never
                touched at all.
  renderers     an entity page's block (Overview, Current facts, Related, Documents), a
                topic page's block, a decision page, a document's summary page, and the
                overview's "At a glance".

Standard library only.
"""
import hashlib
import json
import re
import urllib.parse

import local_facts as F


# =================================================================== escaping ====
def clean_line(text, limit=400):
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


MD_SPECIAL = re.compile(r"([\\`*_\[\]()!#|~])")


def esc(text, limit=400):
    """Text from a document or the model, for a page body: one line, no HTML, and no
    Markdown syntax (links, images, code), so it can only ever show as text."""
    t = clean_line(text, limit).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = MD_SPECIAL.sub(r"\\\1", t)
    # No bare link the site would make clickable, and no %% (a comment that hides text).
    t = re.sub(r"(?i)(https?|ftp|mailto):", r"\1\\:", t)
    t = re.sub(r"(?i)\bwww\.", "www\\.", t)
    return t.replace("@", "\\@").replace("%", "\\%")


def label(text, limit=120):
    """Text for a link label or a page title: one line, no brackets, bars or angle
    brackets (wikilink labels and the site's search results are rendered as HTML)."""
    t = clean_line(text, limit)
    for a, b in (("[", "("), ("]", ")"), ("|", "/"), ("<", "‹"), (">", "›"), ("`", "'"), ("#", "no. ")):
        t = t.replace(a, b)
    return re.sub(r"no\. +", "no. ", t)


def owner_text(text):
    """Text the owner wrote (an Update Packet): Markdown kept, raw HTML and script links not."""
    t = str(text or "").replace("<", "&lt;")
    return re.sub(r"\]\(\s*(?:javascript|data|vbscript):", "](#", t, flags=re.I)


def yq(value):
    """A YAML scalar, always double-quoted (frontmatter written by code never breaks)."""
    return json.dumps(str(value), ensure_ascii=False)


def raw_link(path, text=None):
    """A Markdown link to an original, served read-only by the viewer."""
    return f"[{label(text or path.rsplit('/', 1)[-1])}](/{urllib.parse.quote(path)})"


def cell(link):
    """A wikilink inside a table cell: its | must not end the cell."""
    return link.replace("|", "\\|") if link.startswith("[[") else link


def linked(text, names, limit=600):
    """Escaped text with the first mention of each known name turned into its wikilink.
    names: [(name, link)], longest names first so 'Northwind Trading Sdn Bhd' wins over
    'Northwind Trading'."""
    t = clean_line(text, limit)
    spans = []
    for name, link in sorted(names, key=lambda x: -len(x[0])):
        if len(name) < 3:
            continue
        m = re.search(rf"(?<![^\W_]){re.escape(name)}(?![^\W_])", t, re.I)
        if m and not any(a < m.end() and m.start() < b for a, b, _ in spans):
            spans.append((m.start(), m.end(), link))
    def seg(s):  # escaped, keeping the spaces around a link
        if not s.strip():
            return s
        return (" " if s[0].isspace() else "") + esc(s, limit) + (" " if s[-1].isspace() else "")
    out, pos = [], 0
    for a, b, link in sorted(spans):
        out.append(seg(t[pos:a]))
        out.append(link)
        pos = b
    out.append(seg(t[pos:]))
    return "".join(out)


# =============================================================== engine block ====
START_RE = re.compile(r"<!-- wiki-engine:start sha=([0-9a-f]{12}) -->\n")
END = "<!-- wiki-engine:end -->"


def block_hash(content):
    norm = "\n".join(line.rstrip() for line in content.strip("\n").split("\n"))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def render_block(content):
    content = content.strip("\n")
    return f"<!-- wiki-engine:start sha={block_hash(content)} -->\n{content}\n{END}"


def find_block(body):
    """(start, end, sha, content) of the engine block in a page body, or None."""
    m = START_RE.search(body)
    if not m:
        return None
    e = body.find("\n" + END, m.end() - 1)
    if e == -1:
        return None
    return m.start(), e + 1 + len(END), m.group(1), body[m.end():e]


def block_edited(body):
    b = find_block(body)
    return bool(b) and block_hash(b[3]) != b[2]


def block_section(body, heading):
    """The text under '## heading' inside the engine block, or ''."""
    b = find_block(body)
    if not b:
        return ""
    m = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", b[3], re.M | re.S)
    return m.group(1).strip() if m else ""


def put_block(body, content, where="top"):
    """(new body, outcome): the block replaced, or added at the top or the end of the body.
    outcome is "written", "unchanged" or "edited" (a person changed the block: left as is)."""
    b = find_block(body)
    new = render_block(content)
    if b:
        if block_hash(b[3]) != b[2]:
            return body, "edited"
        if b[3].strip("\n") == content.strip("\n"):
            return body, "unchanged"
        return body[:b[0]] + new + body[b[1]:], "written"
    if where == "top":
        rest = body.lstrip("\n")
        return "\n" + new + ("\n\n" + rest if rest.strip() else "\n"), "written"
    return body.rstrip("\n") + "\n\n" + new + "\n", "written"


# ================================================================== renderers ====
RELATIONS = ["supplier_of", "customer_of", "director_of", "employee_of", "signatory_for", "landlord_of",
             "tenant_of", "owner_of", "subsidiary_of", "partner_of", "adviser_to", "bank_of", "contact_for", "other"]
# (on the subject's page, on the object's page)
REL_TEXT = {
    "supplier_of": ("Supplier of {o}", "{s} — supplier"),
    "customer_of": ("Customer of {o}", "{s} — customer"),
    "director_of": ("Director of {o}", "{s} — director"),
    "employee_of": ("Works at {o}", "{s} — staff"),
    "signatory_for": ("Signs for {o}", "{s} — signatory"),
    "landlord_of": ("Landlord of {o}", "{s} — landlord"),
    "tenant_of": ("Tenant of {o}", "{s} — tenant"),
    "owner_of": ("Owner of {o}", "{s} — owner"),
    "subsidiary_of": ("Subsidiary of {o}", "{s} — subsidiary"),
    "partner_of": ("Partner of {o}", "{s} — partner"),
    "adviser_to": ("Adviser to {o}", "{s} — adviser"),
    "bank_of": ("Bank of {o}", "{s} — bank"),
    "contact_for": ("Contact at {o}", "{s} — contact"),
    "other": ("Related to {o}", "{s}"),
}


REL_SENTENCE = {
    "supplier_of": "{s} supplies {o}", "customer_of": "{s} is a customer of {o}",
    "director_of": "{s} is a director of {o}", "employee_of": "{s} works at {o}",
    "signatory_for": "{s} signs for {o}", "landlord_of": "{s} is the landlord of {o}",
    "tenant_of": "{s} rents from {o}", "owner_of": "{s} owns {o}", "subsidiary_of": "{s} is a subsidiary of {o}",
    "partner_of": "{s} is a partner of {o}", "adviser_to": "{s} advises {o}", "bank_of": "{s} is the bank of {o}",
    "contact_for": "{s} is a contact at {o}", "other": "{s} is related to {o}",
}


def relation_sentence(s, relation, o):
    """'Tan & Partners advises Harbourline Foods.' (plain text, for the model to read)."""
    return REL_SENTENCE.get(relation, REL_SENTENCE["other"]).replace("{s}", s).replace("{o}", o) + "."


def relation_line(a, relation, b):
    """'[[A]] — supplier of [[B]]', for a document's summary page."""
    fwd = REL_TEXT.get(relation, REL_TEXT["other"])[0]
    return f"{a} — " + (fwd[0].lower() + fwd[1:]).replace("{o}", b)


def fact_label(r, title_of):
    t = F.label_of(r["attribute"])
    if r.get("qualifier"):
        t += f" ({clean_line(r['qualifier'], 80)})"
    if r.get("counterparty"):
        t += f", with {title_of(r['counterparty'])}"
    return t


def source_ref(r, link_of):
    """Where a recorded figure came from: its document's summary page, the update it came
    with, or (for a page a person wrote) that page's own source, as text."""
    if r.get("summary"):
        return link_of(r["summary"], r.get("source_title") or "source")
    src = r.get("source") or ""
    return esc(src.rsplit("/", 1)[-1], 120) if src else ""


def facts_table(groups, title_of, link_of, limit=30):
    if not groups:
        return []
    lines = ["| Fact | Value | Source | Date |", "|---|---|---|---|"]
    for g in groups[:limit]:
        cur = g[0]
        value = esc(cur["value"], 160)
        older = [r for r in g[1:] if not F.same_norm(r.get("norm"), cur.get("norm"))]
        if older:
            o = older[0]
            ref = source_ref(o, link_of)
            value += f" (previously {esc(o['value'], 80)}" + (f", {cell(ref)}" if ref else "") + ")"
            if cur.get("supersedes"):
                value += ", changed by an update"
        lines.append(f"| {esc(fact_label(cur, title_of), 160)} | {value} | {cell(source_ref(cur, link_of))} "
                     f"| {cur.get('doc_date') or ''} |")
    if len(groups) > limit:
        lines.append(f"\n_{len(groups) - limit} older facts are in the documents listed below._")
    return lines


SPECIFIC = {"landlord_of", "tenant_of", "adviser_to", "bank_of", "director_of", "employee_of", "signatory_for",
            "contact_for", "owner_of", "subsidiary_of"}


def plausible(rels):
    """The links worth showing: a generic "supplier of" between two parties that also have a
    specific link the other way (a landlord, an adviser, a bank) is a misreading, and so is a
    "supplier of" that fewer documents give than give it the other way round (a refund
    cheque to a customer reads like a payment to a supplier)."""
    specific = {(r["page"], r.get("counterparty")) for r in rels if r.get("qualifier") in SPECIFIC}
    keep = [r for r in rels if not (r.get("qualifier") in ("supplier_of", "customer_of", "other")
                                    and (r.get("counterparty"), r["page"]) in specific)]
    ways = {}
    for r in keep:
        if r.get("qualifier") == "supplier_of" and r.get("counterparty"):
            ways.setdefault((r["page"], r["counterparty"]), set()).add(r.get("source") or r.get("summary"))
    return [r for r in keep if not (r.get("qualifier") == "supplier_of" and len(ways.get(
        (r.get("counterparty"), r["page"]), ())) > len(ways.get((r["page"], r.get("counterparty")), ())))]


def related_lines(page, rels, link_of):
    out = []
    for r in plausible(rels):
        fwd, back = REL_TEXT.get(r.get("qualifier"), REL_TEXT["other"])
        if r["page"] == page and r.get("counterparty"):
            line = "- " + fwd.format(o=link_of(r["counterparty"]))
        elif r.get("counterparty") == page:
            line = "- " + back.format(s=link_of(r["page"]))
        else:
            continue
        if line not in out:
            out.append(line)
    return out


def documents_lines(docs, link_of, limit=40):
    out, seen = [], set()
    for r in sorted(docs, key=lambda r: (r.get("doc_date") or "", r.get("recorded") or ""), reverse=True):
        key = r.get("summary") or r.get("source")
        if key in seen:
            continue
        seen.add(key)
        line = f"- {r.get('doc_date') or 'undated'} — " + (link_of(r["summary"], r.get("source_title"))
                                                           if r.get("summary") else esc(r.get("source_title") or key))
        if r.get("value"):
            line += f" — {esc(r['value'], 160)}"
        out.append(line)
        if len(out) >= limit:
            break
    return out


def entity_block(overview, table, related, documents):
    lines = ["## Overview", "", overview or "", ""]
    if table:
        lines += ["## Current facts", ""] + table + [""]
    if related:
        lines += ["## Related", ""] + related + [""]
    if documents:
        lines += ["## Documents", ""] + documents + [""]
    return "\n".join(lines)


def topic_block(overview, points, link_of, with_overview=True):
    lines = []
    if with_overview:
        lines += ["## Overview", "", overview or "", ""]
    items, seen = [], set()
    for r in sorted(points, key=lambda r: (r.get("doc_date") or "", r.get("recorded") or ""), reverse=True):
        k = F.canon(r.get("value"))
        if k in seen:
            continue
        seen.add(k)
        ref = link_of(r["summary"], "source") if r.get("summary") else esc(r.get("source_title") or "")
        items.append(f"- {r.get('doc_date') or 'undated'} — {esc(r['value'], 300)}" + (f" ({ref})" if ref else ""))
    if items:
        lines += ["## From documents", ""] + items[:60] + [""]
    return "\n".join(lines)


def frontmatter(fields):
    """fields: [(key, rendered value)] in order -> the frontmatter text."""
    return "---\n" + "\n".join(f"{k}: {v}" for k, v in fields) + "\n---\n"
