#!/usr/bin/env python3
"""wiki_menu.py — the wiki's menu, and where each page sits in it.

The local site's left menu shows the wiki in sections, in this order: Company Profile,
Marketing, Sales, Customer Service, Human Resources, Operation, Legal, Account, Finance;
each with its categories. engine/menu.json (an engine file) holds them, with a hint for
each category, and the list of government bodies. Pages never move for the menu: a page
appears under every "section/category" its frontmatter lists,

    menu: ["company-profile/suppliers", "company-profile/customers"]

and pages without one stay in their folder groups below the sections (products,
projects, decisions and updates always do).

How a page gets its menu:

  - A document the engine reads (fast reading, or the model on this Mac): the reader
    chooses one or two categories for its summary page. A new topic page takes its
    starter topic's place, or the one its title names, or the document's.
  - Company and people pages, by rules, from what the engine has recorded about them
    (archive/claims.jsonl): how they deal with the business (a supplier to it, its
    landlord, its bank, a person who works there) and the roles documents give them (tax
    agent, auditor, lawyer, ...). The business's own page, its directors and shareholders
    go under The company itself, its staff under Employees (a job title does not make
    them a party). A person at another company goes where that company is. A government
    body goes under Government bodies only, and has one page whatever name a document
    uses for it: one of its names or aliases exactly, or an official name of three words
    or more inside a longer one. A business whose name merely holds an everyday word of
    an alias ("Hasil Laut Segar", "Bomba Safety Services") is never taken for a body.
  - Topic pages by their title; summary pages by the topic pages they add to, or else
    by their title.
  - What the rules cannot place (summary and topic pages a wiki had before the menu),
    Claude places once, when the wiki reads with Claude. It sees each page's title,
    folder, type and first lines, 60 pages a call, and nothing else.

The engine only ever adds to a page's menu: an entry a person wrote is never removed
(written as its key when it names one in other words, kept as it is when it names
none), and "menu_auto: false" in a page's frontmatter stops it adding to that page's
menu (it still writes a title there as its key, the only form the site reads). Index pages, the overview, pages under wiki/_*, the pages that
explain the wiki, and project, decision, update and product pages never get a menu from
the engine; a person may still give them one, and the site honours it.

Usage (the runner):
  wiki_menu.py label           the rules over the whole wiki (every run's tidy step);
                               also writes .wiki-engine/state/menu-unplaced.txt
  wiki_menu.py label --once    the same, once per version of engine/menu.json (an engine
                               update labels the wiki before its first rebuild)
  wiki_menu.py ask [--limit N] [--yield]
                               Claude places what the rules could not, N pages at most
                               (1500), after the rules have placed what they can. Each
                               page is asked once (menu-asked.txt). It stops at the
                               spending cap per batch, and with --yield when a document
                               arrives. A call that gets no answer stops it for 6 hours
                               (menu-ask-failed); a page unanswered in two such passes is
                               left for a person (menu-ask-tries). Does nothing when the
                               wiki reads with the model on this Mac.
  wiki_menu.py pending         exit 0 when ask has something to do now, else 1
WIKI_MENU_JSON points the module at another menu.json (tests). Standard library only.
"""
import argparse
import json
import os
import re
import signal
import sys
import time
import unicodedata

WIKI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WIKI_DIR, "scripts"))
import local_pages as LP  # noqa: E402
from local_pages import clean_line, fm_get, fm_list, fm_set, same_entity, split_suffix, yq  # noqa: E402

STATE_DIR = os.path.join(WIKI_DIR, ".wiki-engine", "state")
CLAIMS = os.path.join(WIKI_DIR, "archive", "claims.jsonl")
CONFIG = os.path.join(WIKI_DIR, "wiki.config.json")
UNPLACED = os.path.join(STATE_DIR, "menu-unplaced.txt")
ASKED = os.path.join(STATE_DIR, "menu-asked.txt")
ASK_FAILED = os.path.join(STATE_DIR, "menu-ask-failed")
ASK_TRIES = os.path.join(STATE_DIR, "menu-ask-tries")   # pages a pass left unanswered, how often
LABELLED = os.path.join(STATE_DIR, "menu-labelled")
ASK_CHUNK = 60            # pages per Claude call
ASK_LIMIT = 1500          # pages per invocation of ask
ASK_SECONDS = 300         # one call's answer: a minute is usual, this is plenty
ASK_TRIES_MAX = 2         # passes a page may go unanswered before it is left to a person
RETRY_SECONDS = 6 * 3600  # after Claude could not run, ask waits this long
SUMMARY_CHARS = 300       # of a page's first lines, sent with its title
KEY = re.compile(r"[a-z0-9-]+")


def log(msg):
    try:
        import wiki_events
        wiki_events.log(f"menu: {msg}")
    except Exception:
        pass


# ======================================================================== menu ====
def menu_file():
    return os.environ.get("WIKI_MENU_JSON") or os.path.join(WIKI_DIR, "engine", "menu.json")


def load_menu():
    """engine/menu.json as read, or an empty menu when it cannot be read: then no page
    gets a menu, and nothing fails."""
    try:
        with open(menu_file(), encoding="utf-8") as f:
            menu = json.load(f)
    except (OSError, ValueError):
        return {}
    return menu if isinstance(menu, dict) else {}


def _words(text):
    """Casefolded words joined by '-', '&' read as 'and': "Banks & Financiers" is
    "banks-and-financiers", the way keys are written."""
    t = unicodedata.normalize("NFKC", str(text)).casefold().replace("&", " and ")
    return "-".join(re.findall(r"[^\W_]+", t))


MENU = load_menu()
VERSION = MENU.get("version") if isinstance(MENU.get("version"), (int, float, str)) else 0
SECTIONS = []       # [{"key", "title", "categories": [{"key", "title", "hint"}]}], in order
PATHS = []          # every "section/category", in menu order
TITLES = {}         # path -> (section title, category title)
HINTS = {}          # path -> the category's hint
_SECTION_OF = {}    # the section's key or title, in words -> its key
_CATEGORY_OF = {}   # section key -> {the category's key or title, in words -> its key}
for _s in MENU.get("sections") or []:
    if not isinstance(_s, dict) or not KEY.fullmatch(str(_s.get("key") or "")):
        continue
    _sec = {"key": _s["key"], "title": clean_line(_s.get("title") or _s["key"], 80), "categories": []}
    for _c in _s.get("categories") or []:
        if not isinstance(_c, dict) or not KEY.fullmatch(str(_c.get("key") or "")):
            continue
        _path = f"{_sec['key']}/{_c['key']}"
        if _path in TITLES:
            continue
        _cat = {"key": _c["key"], "title": clean_line(_c.get("title") or _c["key"], 80),
                "hint": clean_line(_c.get("hint"), 200)}
        _sec["categories"].append(_cat)
        PATHS.append(_path)
        TITLES[_path] = (_sec["title"], _cat["title"])
        HINTS[_path] = _cat["hint"]
        _CATEGORY_OF.setdefault(_sec["key"], {}).setdefault(_words(_c["key"]), _c["key"])
        _CATEGORY_OF[_sec["key"]].setdefault(_words(_cat["title"]), _c["key"])
    if _sec["categories"]:
        SECTIONS.append(_sec)
        _SECTION_OF.setdefault(_words(_sec["key"]), _sec["key"])
        _SECTION_OF.setdefault(_words(_sec["title"]), _sec["key"])
_ORDER = {p: i for i, p in enumerate(PATHS)}
# "Company Profile/Suppliers", "Company Profile › Suppliers", "account / tax"
_SEPARATOR = re.compile(r"\s*(?:/|›|»|>|→|\\|::)\s*")


def title_of(path):
    """'Company Profile › Suppliers' for "company-profile/suppliers" (the path itself if
    the menu has no such category)."""
    t = TITLES.get(path)
    return f"{t[0]} › {t[1]}" if t else str(path)


def key_of(value):
    """The "section/category" key a menu entry names, in any case or spacing, by keys or
    by titles; None when it names none."""
    if not isinstance(value, str):
        return None
    parts = _SEPARATOR.split(value.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    sec = _SECTION_OF.get(_words(parts[0]))
    cat = _CATEGORY_OF.get(sec, {}).get(_words(parts[1])) if sec else None
    return f"{sec}/{cat}" if cat else None


def normalise(values):
    """Menu entries as keys, in order, each once: a single string counts as a list of one,
    and an entry that names no category is left out."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    out = []
    for v in values:
        k = key_of(v)
        if k and k not in out:
            out.append(k)
    return out


def ordered(paths):
    """Paths in menu order, each once, the unknown ones left out."""
    return sorted({p for p in paths if p in _ORDER}, key=_ORDER.get)


def _short(hint, limit=52):
    """The start of a hint, never cut in the middle of a word."""
    h = (hint or "").strip()
    if len(h) > limit:
        h = h[:limit].rsplit(" ", 1)[0].rstrip(" ,;") + "…"
    return h


def _misleads(category):
    """Whether a category's key alone could mislead a reader: its hint tells the
    business's own side from a supplier's (quotations the business gives, supplier
    bills), another section has a category of the same key, or the key is a short one
    (tax, it, ip, sops)."""
    hint = category["hint"].lower()
    return ("the business" in hint or "supplier" in hint or len(category["key"]) <= 4
            or sum(c["key"] == category["key"] for s in SECTIONS for c in s["categories"]) > 1)


def listing(compact=True):
    """The menu as text for a question. Full: one line per category, its key, titles and
    hint. Compact (about 400 tokens): one line per section, its categories' keys, each
    with the start of its hint where the key alone could mislead; a value is the
    section's key, "/", and the category's key."""
    if not compact:
        return "\n".join(f"- {p} — {title_of(p)}" + (f" ({HINTS[p]})" if HINTS[p] else "") for p in PATHS)
    lines = ['Each value is "section/category", for example "account/tax".']
    for s in SECTIONS:
        cats = ", ".join(c["key"] + (f" ({_short(c['hint'])})" if c["hint"] and _misleads(c) else "")
                         for c in s["categories"])
        lines.append(f"- {s['key']}: {cats}")
    return "\n".join(lines)


# ============================================================ government bodies ====
def _gnorm(text):
    """A name's words, casefolded, '&' read as 'and', punctuation dropped."""
    t = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("&", " and ")
    return " ".join(re.findall(r"[^\W_]+", t))


_GOV = MENU.get("government") if isinstance(MENU.get("government"), dict) else {}
BODIES = []         # [(canonical name, [aliases])]
for _b in _GOV.get("bodies") or []:
    if isinstance(_b, dict) and isinstance(_b.get("name"), str) and _b["name"].strip():
        _al = [a.strip() for a in _b.get("aliases") or [] if isinstance(a, str) and a.strip()]
        BODIES.append((_b["name"].strip(), list(dict.fromkeys(_al))))


def _core(words):
    """A body's name in words without "Malaysia" (or "of Malaysia", "(M)") at its end, so
    "Bank Negara" is "Bank Negara Malaysia"."""
    return re.sub(r"(?:\s+of)?\s+(?:malaysia|m)$", "", words)


# Per body: its names and aliases in words (an exact name), and its acronyms of four
# capitals or more (LHDN, KWSP), which place a page that nothing else places.
_BODY_NAMES = []
for _name, _al in BODIES:
    _words_of = [_gnorm(n) for n in [_name] + _al]
    _BODY_NAMES.append((_name, {_core(w) for w in _words_of} | set(_words_of),
                        [a for a in _al if re.fullmatch(r"[A-Z][A-Z0-9]{3,}", a)]))
_PATTERNS = [_gnorm(p) for p in _GOV.get("patterns") or [] if isinstance(p, str) and _gnorm(p)]
# A business, a club or a co-op is named like one, wherever the word stands: "Hasil Tani
# PLT", "Kedai Hasil Laut", "Bomba Safety Services", "Swift Customs Brokerage", "Kantin
# Jabatan Kastam", or a registration number in brackets. Such a name is never placed under
# Government bodies by its looks; only a body's exact name is one (Pembangunan Sumber
# Manusia Berhad is).
_COMPANY_MARK = re.compile(
    r"(?<!\S)(?:sdn|bhd|berhad|plt|llp|llc|ltd|limited|pte|inc|plc|gmbh|corp|services|resources|holdings?|"
    r"enterprises?|trading|traders?|kedai|restoran|restaurant|cafe|café|bakery|catering|kitchen|supplies|"
    r"suppliers?|stores?|mart|shop|agency|agencies|agents?|ejen|brokers?|brokerage|forwarding|forwarders?|"
    r"logistics|consultancy|consultants?|consulting|solutions|industries|ventures|group|partners|associates|"
    r"lawyers|advocates|solicitors|syarikat|perniagaan|pemborong|and co|kantin|canteen|koperasi|cooperative|"
    r"co operative|kelab|club|persatuan|association|society)(?!\S)|(?:^|\s)co$")


def company_named(name):
    """Whether a name is written like a business's (a company marker anywhere in it)."""
    raw = str(name or "")
    if re.search(r"\(\s*[A-Za-z]{0,3}\s*\d[\d\s-]{3,}[A-Za-z]?\s*\)", raw) or re.search(r"\b(?:19|20)\d{10}\b", raw):
        return True
    return _COMPANY_MARK.search(_gnorm(raw)) is not None


def _phrase_in(phrase, words):
    return re.search(rf"(?<!\S){re.escape(phrase)}(?!\S)", words) is not None


def listed_exact(name):
    """The listed body a name is exactly (one of its names or aliases, give or take a
    "Malaysia" at the end), or None."""
    n = _gnorm(name)
    if not n:
        return None
    return next((canonical for canonical, exact, _ in _BODY_NAMES if n in exact or _core(n) in exact), None)


def government_page(name):
    """The listed body whose page a party named so belongs on, or None: only when the
    name is exactly one of the body's names or aliases (give or take a "Malaysia" at the
    end), or a pair of them, one in brackets ("Inland Revenue Board (LHDN)"). Anything
    more ("LHDN Cawangan Pudu", "Koperasi Kakitangan Lembaga Hasil Dalam Negeri", "Hasil
    Sdn Bhd") is a page of its own: a business, a club or a branch never takes a body's
    page, its bank account with it."""
    exact = listed_exact(name)
    if exact:
        return exact
    raw = str(name or "")
    parts = [x for x in re.findall(r"\(([^()]*)\)", raw) if _gnorm(x) not in ("", "m")]
    outer = re.sub(r"\([^()]*\)", " ", raw)
    if parts and _gnorm(outer):
        found = {listed_exact(x) for x in [outer] + parts}
        if len(found) == 1 and None not in found:
            return found.pop()
    return None


def government_body(name):
    """Whether a name looks like a government body's, for placing a page that nothing else
    places: a listed body's canonical name (as government_page() finds it, or by one of
    its acronyms as a whole word: "LHDN Cawangan Pudu", "Lhdn Cawangan Pudu" as a
    letterhead's capitals are tidied), the name itself for a body the list does not have
    (a ministry, a city council, customs, ...), or None. A name written like a business's
    (or a club's, a co-op's) is only ever an exact match."""
    page = government_page(name)
    if page:
        return page
    raw, n = str(name or ""), _gnorm(name)
    for canonical, exact, _ in _BODY_NAMES:
        # An official name of three words or more in a longer one, the rest no business's
        # or club's words: a branch ("Suruhanjaya Syarikat Malaysia Cawangan Pulau Pinang").
        for w in exact:
            if len(w.split()) >= 3 and _phrase_in(w, n):
                rest = re.sub(rf"(?<!\S){re.escape(w)}(?!\S)", " ", n)
                if not company_named(rest):
                    return canonical
    if company_named(name):
        return None
    for canonical, _, acronyms in _BODY_NAMES:
        if any(re.search(rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])", raw, re.I) for a in acronyms):
            return canonical
    if any(_phrase_in(p, n) for p in _PATTERNS):
        return clean_line(name, 120)
    return None


def government_aliases(canonical):
    """Every other name of a body the menu lists (none for one it does not list)."""
    return next((list(al) for name, al in BODIES if name == canonical), [])


def listed_body(canonical):
    return any(name == canonical for name, _ in BODIES)


# ================================================================= the rules ====
# The topic pages a small business usually keeps (local_engine.TOPICS), and where each
# sits in the menu: the reader offers these to every document, and the rules place them
# by title.
STARTER_TOPICS = [
    ("finance-legal", "Sales and service tax (SST)", ["account/tax"]),
    ("finance-legal", "Income tax and the tax agent", ["account/tax"]),
    ("finance-legal", "Supplier contracts and terms", ["legal/contracts", "operation/purchasing"]),
    ("finance-legal", "Customer contracts and terms", ["legal/contracts", "sales/orders"]),
    ("finance-legal", "Banking and bank reconciliations", ["account/reconciliations", "finance/facilities"]),
    ("finance-legal", "Payroll and statutory contributions", ["human-resources/payroll"]),
    ("finance-legal", "Premises and leases", ["operation/premises"]),
    ("finance-legal", "Insurance", ["finance/insurance"]),
    ("finance-legal", "Loans and financing", ["finance/loans"]),
    ("finance-legal", "Invoicing and receivables", ["account/receivables"]),
    ("finance-legal", "Purchasing and payables", ["account/payables", "operation/purchasing"]),
    ("finance-legal", "Licences, permits and registrations", ["legal/licences"]),
    ("finance-legal", "Board and company secretarial", ["legal/secretarial"]),
    ("finance-legal", "Financial results and budgets", ["account/audit", "finance/budgets"]),
    ("how-it-runs", "Month-end close", ["account/bookkeeping"]),
    ("how-it-runs", "Stock and inventory counts", ["operation/inventory"]),
    ("how-it-runs", "Approving payments", ["account/payables"]),
    ("how-it-runs", "Expansion and new outlets", ["operation/premises"]),
    ("how-it-runs", "Staff, roles and rostering", ["human-resources/policies", "human-resources/leave"]),
    ("how-it-runs", "Quality and supplier performance", ["operation/purchasing"]),
    ("how-it-runs", "Customer service", ["customer-service/enquiries"]),
    ("how-it-runs", "Equipment and maintenance", ["operation/equipment"]),
]


def _slug(text):
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


_STARTER_BY_SLUG = {_slug(t): paths for _, t, paths in STARTER_TOPICS}

# Words in a title that say where a page belongs: (pattern, paths, also for a document's
# title). The first pattern that matches decides, so the specific come before the
# general: "insurance policy" is insurance, not a staff policy, and "payment voucher" is
# a supplier payment, not a promotion. A word that is just as often the business's own as
# a supplier's (a quotation, an order, an invoice) does not place a document by itself.
KEYWORDS = [(re.compile(rf"\b(?:{rx})", re.I), paths, docs) for rx, paths, docs in [
    (r"insurance|insurer|takaful", ["finance/insurance"], True),
    (r"suppl(?:y|ier) (?:contracts?|agreements?|terms)", ["legal/contracts", "operation/purchasing"], True),
    (r"(?:customer|sales) (?:contracts?|agreements?|terms)", ["legal/contracts", "sales/orders"], True),
    (r"employment (?:contracts?|agreements?)|offer letters?|appointment letters?|letters? of (?:offer|appointment)|"
     r"resignations?", ["human-resources/contracts"], True),
    # A lease is of the premises, unless it is of a vehicle or equipment (financing).
    (r"(?:car|vehicle|motor|equipment|machinery|machine|forklift|lorry|lorries|van|truck|copier|printer)s? "
     r"(?:leases?|leasing)|(?:hire[- ]purchase|finance) leases?", ["finance/loans"], True),
    (r"tenanc(?:y|ies)|leases?\b|rental agreements?|premises|outlets?|renovations?|utilit(?:y|ies)|expansion",
     ["operation/premises"], True),
    (r"hire[- ]purchase|loans?\b|financing|borrowings?|repayments?", ["finance/loans"], True),
    (r"overdrafts?|banking facilit(?:y|ies)|credit (?:lines?|facilit(?:y|ies))|bank guarantees?",
     ["finance/facilities"], True),
    (r"bank statements?|reconciliations?|banking\b", ["account/reconciliations"], True),
    (r"sst\b|sales (?:and|&) services? tax|service tax|sales tax|income tax|withholding tax|"
     r"tax (?:returns?|filings?|computations?|payments?|agents?|estimates?)|tax\b(?!\s+invoices?\b)",
     ["account/tax"], True),
    (r"audit|financial (?:statements?|results?|reports?)|management accounts?|annual accounts?|"
     r"profit (?:and|&) loss|balance sheets?", ["account/audit"], True),
    (r"budgets?|forecasts?|projections?", ["finance/budgets"], True),
    (r"cash ?flow|cash position", ["finance/cash-flow"], True),
    (r"grants?\b|incentives?|subsid(?:y|ies)", ["finance/grants"], True),
    (r"investments?|fixed assets?|capital expenditure|capex", ["finance/investments"], True),
    (r"payroll|pay ?slips?|salar(?:y|ies)|wages?|epf\b|kwsp\b|socso\b|perkeso\b|eis\b|pcb\b|hrdf\b|"
     r"statutory contributions?", ["human-resources/payroll"], True),
    (r"recruit|hiring|onboarding|job (?:adverts?|ads?|applications?|descriptions?)|interviews?|vacanc(?:y|ies)",
     ["human-resources/recruitment"], True),
    (r"leave\b|attendance|rosters?|rostering|overtime|shifts?\b|timesheets?", ["human-resources/leave"], True),
    (r"training|courses?\b", ["human-resources/training"], True),
    (r"appraisals?|performance reviews?|warning letters?|disciplin|misconduct", ["human-resources/performance"], True),
    (r"work permits?|foreign workers?|visas?\b|levy\b|levies", ["human-resources/permits"], True),
    (r"(?:staff|employee|hr) (?:handbook|polic(?:y|ies)|rules)|handbook|code of conduct",
     ["human-resources/policies"], True),
    (r"board (?:meetings?|minutes|resolutions?|of directors|papers?)|(?:directors|shareholders|members)'? "
     r"(?:meetings?|minutes|resolutions?|circulars?)|minutes of (?:the )?(?:board|directors|shareholders|agm|egm|"
     r"annual general)|(?:circular|written) resolutions?|agm\b|egm\b|annual general meeting|company secretar|"
     r"secretarial|annual returns?|ssm\b", ["legal/secretarial"], True),
    (r"licen[cs]es?|permits?\b|registrations?|certificat|halal", ["legal/licences"], True),
    (r"trade ?marks?|patents?|copyrights?|intellectual property|domain names?", ["legal/ip"], True),
    (r"disputes?|demand letters?|letters? of demand|litigation|court\b|tribunal|lawsuits?", ["legal/disputes"], True),
    (r"pdpa\b|personal data|data protection|privacy|compliance", ["legal/compliance"], True),
    (r"contracts?\b|agreements?\b", ["legal/contracts"], True),
    (r"(?:month|year|period)[- ]end|closing (?:of )?(?:the )?(?:accounts|books)|bookkeeping|ledgers?\b|"
     r"journals?\b|accounting records|chart of accounts", ["account/bookkeeping"], True),
    (r"receivables?|debtors?|sales invoices?|invoicing|collections?\b", ["account/receivables"], True),
    (r"credit notes?", ["account/receivables"], False),
    (r"payables?|creditors?|supplier (?:invoices?|bills?)|payment vouchers?|approving payments?|"
     r"payment approvals?", ["account/payables"], True),
    (r"bills?\b|payments?\b", ["account/payables"], False),
    (r"stock\b|stocktak|inventor(?:y|ies)|wastage", ["operation/inventory"], True),
    (r"equipment|machines?\b|machinery|vehicles?|maintenance|repairs?\b|asset register", ["operation/equipment"], True),
    (r"purchas|procurement|suppliers?\b|vendors?\b|sourcing", ["operation/purchasing"], True),
    (r"deliver(?:y|ies)|logistics|shipping|shipments?|couriers?|freight", ["operation/logistics"], True),
    (r"software|computers?|laptops?|devices?|point of sale|pos system|passwords?|it support|internet|wi-?fi",
     ["operation/it"], True),
    (r"safety|hygiene|inspections?|incidents?|fire\b|sanitation|food handling", ["operation/safety"], True),
    (r"sops?\b|procedures?|processes\b|checklists?|workflows?|standard operating|how to\b", ["operation/sops"], True),
    (r"complaints?|enquir(?:y|ies)|inquir(?:y|ies)|customer service|customer care", ["customer-service/enquiries"], True),
    (r"warrant(?:y|ies)|refunds?|returns? polic(?:y|ies)|(?:goods|product|item|customer) exchanges?",
     ["customer-service/returns"], True),
    (r"slas?\b|service levels?|response times?", ["customer-service/service-levels"], True),
    (r"feedback|(?:customer|online|google|guest) reviews?|ratings?\b|testimonials?", ["customer-service/feedback"],
     True),
    (r"faqs?\b|frequently asked", ["customer-service/faqs"], True),
    (r"after[- ]sales|servicing", ["customer-service/after-sales"], True),
    (r"price lists?|pricing|rate cards?|prices\b", ["sales/price-lists"], True),
    (r"tenders?\b|bids?\b|proposals?", ["sales/tenders"], True),
    (r"sales (?:reports?|figures|targets?|performance)|revenue", ["sales/reports"], True),
    (r"commissions?|distributors?|resellers?|dealers?\b|sales agents?", ["sales/agents"], True),
    (r"quotations?|quotes?\b", ["sales/quotations"], False),
    (r"orders?\b", ["sales/orders"], False),
    (r"brand|logos?\b", ["marketing/brand"], True),
    (r"campaigns?|(?<!staff )(?<!job )promotions?|promos?\b|discounts?|vouchers?", ["marketing/campaigns"], True),
    (r"social media|websites?|facebook|instagram|tiktok|online\b|digital|seo\b", ["marketing/digital"], True),
    (r"market research|competitors?|market stud(?:y|ies)|surveys?", ["marketing/research"], True),
    (r"events?\b|launch(?:es)?\b|trade (?:fairs?|shows?)|sponsorships?|exhibitions?", ["marketing/events"], True),
    (r"brochures?|flyers?|catalogues?|signage|menus?\b|posters?", ["marketing/materials"], True),
    (r"(?:marketing|advertising|design|media|creative|digital|pr) agenc(?:y|ies)", ["marketing/agencies"], True),
    (r"marketing|advertis", ["marketing/campaigns"], True),
]]


def keyword_paths(text, doc=False):
    """Where a title says a page belongs: the paths of the first pattern that matches
    (doc: a document's title and type, which the words that could be either side's
    do not place). [] when none does."""
    t = unicodedata.normalize("NFKC", str(text or "")).replace("_", " ")
    for rx, paths, for_docs in KEYWORDS:
        if (for_docs or not doc) and rx.search(t):
            return [p for p in paths if p in _ORDER]
    return []


def topic_paths(title):
    """Where a topic page belongs by its title: a starter topic's place, else what its
    words say."""
    starter = _STARTER_BY_SLUG.get(_slug(title))
    if starter:
        return [p for p in starter if p in _ORDER]
    return keyword_paths(title)


# A party's role in the documents (and, on a page a person wrote, its tags) says what it
# is to the business. Matched case-insensitively at the start of a word.
ROLES = [(cat, re.compile(rf"\b(?:{rx})", re.I)) for cat, rx in [
    ("company-secretary", r"company secretar|secretarial|setiausaha"),
    ("auditor", r"audit"),
    ("tax-agent", r"tax agent|tax advis|tax consult"),
    ("accountant", r"accountant|accounting firm|bookkeep"),
    ("lawyer", r"lawyer|solicitor|advocate|legal\b|law firm|peguam"),
    ("contractors", r"contractor|sub-?contractor|freelancer|outsourc|manpower"),
    ("insurers", r"insur(?!ed\b)|takaful|underwrit"),
    ("banks", r"bank(?:s|er|ers)?\b(?!\s+(?:account|details|transfer|statement|charges?|draft|slip|in)\b)|"
              r"financier|lender|leasing|hire purchase|financing"),
    ("landlords", r"landlord|lessor"),
    ("suppliers", r"supplier|vendor|seller|supplies"),
    ("customers", r"customer|client|buyer|purchaser"),
    ("government", r"government|regulator|authority\b|statutory body|council"),
]]
PERSON_ROLES = [("company", re.compile(r"\b(?:shareholder|director)", re.I)),
                ("employees", re.compile(r"\b(?:employee|staff)\b", re.I))]
# A director or shareholder plainly called so, not a job title with the word in it.
_PLAIN = r"(?:(?:managing|executive|non-executive|independent|sole|company)\s+)?(?:director|shareholder|pengarah)s?"
PLAIN_PARTY_ROLE = re.compile(rf"\s*(?:(?:the|a|our)\s+)?{_PLAIN}(?:\s*(?:and|&|/|,)\s*{_PLAIN})*\s*", re.I)


TRADE = {"customers", "suppliers"}
PARTY = "company-profile/"
PERSON_TAGS = {"person", "people", "individual", "employee", "staff", "director", "shareholder"}
SEED_PAGES = {"how-this-wiki-works", "raw-to-markdown-conversion", "ingestion-register"}
OWN_FOLDERS = {"projects", "decisions", "updates", "products"}   # their own groups in the menu


def plain_party_role(text):
    """Whether a role is plainly a director's or a shareholder's (PLAIN_PARTY_ROLE)."""
    return PLAIN_PARTY_ROLE.fullmatch(str(text or "").replace("-", " ").replace("_", " ")) is not None


def never(rel):
    """Pages the engine never gives a menu: index pages, the overview, pages under wiki/_*,
    the pages that explain the wiki, and projects, decisions, updates and products."""
    parts = rel.split("/")
    name = parts[-1][:-3] if parts[-1].endswith(".md") else parts[-1]
    return (len(parts) < 2 or parts[0] != "wiki" or name == "index" or rel == "wiki/overview.md"
            or any(p.startswith((".", "_")) for p in parts[1:]) or name in SEED_PAGES
            or (len(parts) > 2 and parts[1] in OWN_FOLDERS))


def kind_of(rel):
    """What the rules see a page as: entity, topic, source, or "" (none of them)."""
    if never(rel):
        return ""
    folder = rel.split("/")[1] if rel.count("/") >= 2 else ""
    return {"companies": "entity", "finance-legal": "topic", "how-it-runs": "topic",
            "sources": "source"}.get(folder, "")


# ================================================================= pages =========
# The frontmatter block, empty or not: "---", its lines, "---".
_FRONT = re.compile(r"\A(\ufeff?)---[ \t]*(\r?\n)((?:.*?\r?\n)??)---[ \t]*(\r?\n|\Z)", re.S)
# A line that starts a key at column 0: "title: x", '"title": x', "Last updated: x". An
# indented line, a list item or a flow map is not one (the menu line could not join it).
_KEY_LINE = re.compile(r"""^(?:"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s"'#\-\[\]{}&*!|>%@`,?:][^:#]*?)"""
                       r"""[ \t]*:(?:[ \t]|$)""")


def _uncomment(value):
    """A YAML value without a comment after it: "a/b  # note" is "a/b", '"a/b"  # note' is
    '"a/b"', "[a/b]  # note" is "[a/b]"."""
    if value[:1] == '"':
        m = re.match(r'"(?:[^"\\]|\\.)*"', value)
    elif value[:1] == "'":
        m = re.match(r"'(?:[^']|'')*'", value)
    elif value[:1] == "[":
        m = re.match(r"\[[^\]]*\]", value)
    else:
        return re.sub(r"\s+#.*$", "", value)
    if m and re.fullmatch(r"(?:\s+#.*)?\s*", value[m.end():]):
        return value[:m.end()]
    return value


def _uncommented_menu(fm):
    """The frontmatter with a comment after a menu entry taken off ("- a/b  # they also
    buy from us", '- "a/b"  # mine'), so the entry is read as it is meant."""
    m = re.search(r"^menu:", fm, re.M)
    if not m:
        return fm
    lines = fm[m.start():].split("\n")
    for i, line in enumerate(lines):
        if i and not re.match(r"^([ \t]+\S|-[ \t]|-$)", line):
            break
        k = re.match(r"^(menu:[ \t]*|[ \t]*-[ \t]*)(.*)$", line)
        if k and k.group(2)[:1] != "{":
            lines[i] = k.group(1) + _uncomment(k.group(2))
    return fm[:m.start()] + "\n".join(lines)


class Page:
    """One wiki page as the rules see it: its frontmatter (split from a body that is
    never changed), title, aliases, tags and menu."""

    def __init__(self, rel, text):
        self.rel, self.text = rel, text
        m = _FRONT.match(text)
        fm = m.group(3).replace("\r\n", "\n")[:-1] if m and m.group(3) else ""
        first = next((l for l in fm.split("\n") if l.strip() and not l.lstrip().startswith("#")), "")
        if m and first and not _KEY_LINE.match(first):
            m = None   # not a map of keys (a note between two rules): never written into
        self.match = m
        self.fm = fm
        self.title = (fm_get(self.fm, "title") or os.path.basename(rel)[:-3]) if m else os.path.basename(rel)[:-3]
        self.aliases = [a for a in (fm_list(self.fm, "aliases") or []) if a] if m else []
        self.tags = [t.lower() for t in (fm_list(self.fm, "tags") or [])] if m else []
        self.menu = fm_list(_uncommented_menu(self.fm), "menu") if m else []   # None: not ours to rewrite
        auto = re.sub(r"\s+#.*$", "", fm_get(self.fm, "menu_auto") if m else "").strip().strip("\"'").lower()
        self.auto = auto not in ("false", "no", "off", "0")

    @property
    def body(self):
        return self.text[self.match.end():] if self.match else self.text

    def valid(self):
        """The categories its menu names."""
        return normalise(self.menu or [])

    def kind(self):
        """company, person, or "" (a product, a place)."""
        tags = set(self.tags)
        if "person" in tags or (tags & PERSON_TAGS and "company" not in tags):
            return "person"
        if tags & {"product", "place"}:
            return ""
        return "company"

    def with_menu(self, paths):
        """The page's text with its menu set to paths, or None when nothing changes: the
        frontmatter alone changes, the body stays byte for byte as it was (line endings
        included)."""
        if not self.match or self.menu is None:
            return None
        if list(paths) == list(self.menu):
            return None
        nl = self.match.group(2)
        line = "menu: [" + ", ".join(yq(p) for p in paths) + "]"
        fm = fm_set(self.fm, "menu", line) if self.fm.strip() else line
        return (self.match.group(1) + "---" + nl + fm.replace("\n", nl) + nl + "---"
                + (self.match.group(4) or nl) + self.body)


def read_page(rel):
    """A page, or None when it cannot be read exactly (not UTF-8: it is never rewritten)."""
    try:
        with open(os.path.join(WIKI_DIR, rel), encoding="utf-8", newline="") as f:
            return Page(rel, f.read())
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def scan():
    """Every page of the wiki, by path (wiki/...), read once."""
    pages = {}
    root = os.path.join(WIKI_DIR, "wiki")
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_")))
        for fn in sorted(files):
            if fn.endswith(".md") and not fn.startswith((".", "_")):
                rel = os.path.relpath(os.path.join(dirpath, fn), WIKI_DIR).replace(os.sep, "/")
                page = read_page(rel)
                if page:
                    pages[rel] = page
    return pages


def write_page(rel, text):
    """A page written whole or not at all: a write that fails (a full disk, a signal)
    leaves no half-written copy beside it, for the site or the history to pick up."""
    full = os.path.join(WIKI_DIR, rel)
    tmp = full + ".menu-tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, full)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def remove_stale_tmp():
    """Copies a write left behind when it was cut off (the power going, say)."""
    for dirpath, dirs, files in os.walk(os.path.join(WIKI_DIR, "wiki")):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if fn.endswith(".md.menu-tmp"):
                try:
                    os.remove(os.path.join(dirpath, fn))
                except OSError:
                    pass


def merged(existing, adds):
    """A page's menu with adds: its own entries first, each written as the key it names
    (a title, other case or spacing) or kept as it is when it names none, then the new
    ones. Nothing is ever taken out."""
    out = []
    for v in existing or []:
        v = key_of(v) or v
        if isinstance(v, str) and v.strip() and v not in out:
            out.append(v)
    for p in adds:
        if p not in out:
            out.append(p)
    return out


def config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def load_claims():
    import local_facts
    return local_facts.Claims(CLAIMS)


class Rules:
    """The rules over a set of pages, with what the claims store records about them
    indexed once (a wiki of thousands of pages is labelled in one pass)."""

    def __init__(self, pages, claims=None, own=None, company=None):
        self.pages = pages
        self.rels, self.roles = {}, {}
        for r in (claims.items + claims.pending) if claims is not None else []:
            a = r.get("attribute")
            if a == "_rel" and r.get("page"):
                for p in {r["page"], r.get("counterparty")}:
                    if p:
                        self.rels.setdefault(p, []).append(r)
            elif a == "_doc" and r.get("page") and r.get("value"):
                self.roles.setdefault(r["page"], []).append(str(r["value"]))
        self.own = own if own is not None else self.find_own(company)
        self.memo = {}

    def find_own(self, company):
        """The business's own page: one tagged own-company; else the company page whose
        title or an alias names the company in wiki.config.json (or starts with it, when
        Settings has a short form, "Harbourline" for "Harbourline Foods Sdn Bhd", and only
        one page does); else none. Never guessed from who works where: a supplier with
        more people on record is not the business."""
        firms = [rel for rel, p in sorted(self.pages.items()) if kind_of(rel) == "entity" and p.kind() == "company"]
        tagged = [rel for rel in firms if "own-company" in self.pages[rel].tags]
        if tagged:
            return tagged[0]
        names = lambda rel: [self.pages[rel].title] + self.pages[rel].aliases  # noqa: E731
        if company:
            hits = [rel for rel in firms if any(same_entity(company, n) for n in names(rel))]
            if hits:
                return hits[0]
            short = split_suffix(company)[0]
            starts = [rel for rel in firms if short and any(
                (split_suffix(n)[0] + " ").startswith(short + " ") for n in names(rel))]
            if len(starts) == 1:
                return starts[0]
        return ""

    def derive(self, rel):
        """The paths the rules give one page ([] for a page they give none)."""
        if rel not in self.memo:
            self.memo[rel] = []   # a loop of people and companies naming each other ends here
            kind, page = kind_of(rel), self.pages.get(rel)
            if page is None or not kind:
                out = []
            elif kind == "entity":
                out = self.entity(rel, page)
            elif kind == "topic":
                out = topic_paths(page.title)
            else:
                out = self.source(page)
            self.memo[rel] = ordered(out) if kind == "entity" else [p for p in out if p in _ORDER]
        return self.memo[rel]

    def role_cats(self, rel, page, person):
        """What the roles documents give a party (and its tags) say it is. In one role, the
        other party it works for is not what it is: "solicitors for the buyer" is a lawyer,
        "the supplier's bank" a bank, "director of the supplier" one of the supplier's
        people, not of the business."""
        cats = set()
        for t in self.roles.get(rel, []) + [t.replace("-", " ").replace("_", " ") for t in page.tags]:
            found = {cat for cat, rx in ROLES if rx.search(t)}
            if found - TRADE:
                found -= TRADE
            if person and not found:
                found = {cat for cat, rx in PERSON_ROLES if rx.search(t)}
            cats |= found
        return cats

    def entity(self, rel, page):
        kind = page.kind()
        if not kind:
            return []   # products and places
        if rel == self.own:
            return [PARTY + "company"]
        person = kind == "person"
        # Government: a page titled exactly as a listed body, or carrying one of its names
        # as an alias, is there only. The engine's own short alias ("KWSP" for "KWSP Sdn
        # Bhd") never counts, nor any alias of a page titled like a business; a title
        # that only looks like a body's (an acronym in it, a generic word like "customs")
        # places a page only when nothing else does.
        if not person and (government_page(page.title) or self.body_alias(page)):
            return [PARTY + "government"]
        looks_official = not person and government_body(page.title)
        cats, linked, own = set(), set(), self.own
        # The relations the page itself shows (local_pages.plausible): a "supplier of" the
        # documents state the other way round more often is a misreading, here as there.
        for r in LP.plausible(self.rels.get(rel, [])):
            q, s, o = r.get("qualifier"), r.get("page"), r.get("counterparty")
            if own and s == rel and o == own:
                cats |= {"supplier_of": {"suppliers"}, "customer_of": {"customers"}, "landlord_of": {"landlords"},
                         "tenant_of": {"customers"}, "bank_of": {"banks"},
                         "employee_of": {"employees"} if person else set(),
                         "director_of": {"company"} if person else set(),
                         "owner_of": {"company"}}.get(q, set())
            elif own and o == rel and s == own:
                cats |= {"supplier_of": {"customers"}, "customer_of": {"suppliers"}, "tenant_of": {"landlords"},
                         "landlord_of": {"customers"}}.get(q, set())
            elif person and s == rel and o and o != own and q in (
                    ("employee_of", "contact_for", "signatory_for", "director_of") if own else
                    ("contact_for", "signatory_for")):
                # Without the business's page known, an employer could be the business
                # itself: only a contact or signatory is surely someone else's.
                linked.add(o)
        roles = self.role_cats(rel, page, person)
        if person and ("employees" in cats or ("employees" in roles and not linked)):
            # The business's own staff (employed by it, or called staff with no other
            # company on record): a job title ("Customer Service Executive", "Sales
            # Director") says what they do, not what they are to the business. Only a
            # company secretary adds, and a director or shareholder plainly called so.
            roles = (roles & {"company-secretary", "employees"}) | (
                {"company"} if self.says_plainly(rel, page) else set())
        elif linked:
            # A person at another company goes where that company is; their own job title
            # counts only when that company is nowhere in the menu.
            inherited = set()
            for other in sorted(linked):
                paths = self.derive(other) + (self.pages[other].valid() if other in self.pages else [])
                inherited |= {p[len(PARTY):] for p in paths if p.startswith(PARTY)} - {"company", "employees"}
            roles = inherited or roles - {"company", "employees"}
        if "government" in roles and (cats | roles) - {"government"}:
            # A role that says government places a party there only when nothing else
            # is said of it: beside "supplier" (of electricity, say) it is a supplier.
            roles.discard("government")
        cats |= roles
        if looks_official and not cats:
            cats = {"government"}
        return [PARTY + c for c in cats]

    def says_plainly(self, rel, page):
        """Whether a role a document gives the page (or a tag) is plainly a director's or a
        shareholder's ("Director", "Managing Director", "director and shareholder"), not a
        job title with the word in it ("Sales Director", "Director of Operations")."""
        return any(plain_party_role(t) for t in self.roles.get(rel, []) + page.tags)

    def body_alias(self, page):
        """Whether the page carries a listed body's name as an alias it was given for that
        body: never the engine's short alias of its own title, and none on a page titled
        like a business ("KWSP Sdn Bhd", "JKDM (M) Sdn Bhd")."""
        if company_named(page.title):
            return False
        short = _gnorm(split_suffix(page.title)[0])
        return any(listed_exact(a) and _gnorm(a) != short for a in page.aliases)

    def source(self, page):
        """A summary page without a menu: where the topic pages it adds to are (two at
        most), else where its title and type say (one)."""
        if page.valid():
            return []
        out = []
        for target in topics_linked(page.body):
            t = self.pages.get(f"wiki/{target}.md")
            if t is not None and kind_of(t.rel) == "topic":
                for p in t.valid() + self.derive(t.rel):
                    if p not in out:
                        out.append(p)
        if out:
            return out[:2]
        return keyword_paths(page.title + " " + " ".join(page.tags), doc=True)[:1]


def topics_linked(body):
    """The pages linked under a page's "## Topics" heading."""
    m = re.search(r"^## Topics[ \t]*\r?\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    if not m:
        return []
    out = []
    for t in re.findall(r"\[\[([^\]|#]+)", m.group(1)):
        t = t.strip().strip("/")
        t = t[len("wiki/"):] if t.startswith("wiki/") else t
        t = t[:-3] if t.endswith(".md") else t
        if t and t not in out:
            out.append(t)
    return out


def derive(rel, page_fm, claims=None, own=None):
    """The paths the rules give one page, its frontmatter as given (the rest of the wiki
    as it is on disk). label() does every page in one pass."""
    pages = scan()
    body = pages[rel].body if rel in pages else ""
    pages[rel] = Page(rel, "---\n" + str(page_fm or "").strip("\n") + "\n---\n" + body)
    return Rules(pages, claims if claims is not None else load_claims(), own=own,
                 company=config().get("company")).derive(rel)


def unplaced(pages, asked=()):
    """Summary and topic pages the rules could not place and Claude was never asked about
    (a page whose menu a person keeps to themselves is not asked about either)."""
    return [rel for rel, p in sorted(pages.items()) if kind_of(rel) in ("source", "topic")
            and p.match and p.auto and p.menu is not None and not p.valid() and rel not in asked]


def label(rels=None, claims=None, pages=None):
    """The rules over the given pages (all of them by default): each page's menu gets the
    paths the rules give it, its own entries first. Only changed pages are written, and
    only their frontmatter. Returns counts."""
    counts = {"pages": 0, "changed": 0}
    if not PATHS:
        return counts
    pages = pages if pages is not None else scan()
    rules = Rules(pages, claims if claims is not None else load_claims(), company=config().get("company"))
    targets = sorted(pages) if rels is None else [r for r in dict.fromkeys(rels) if r in pages]
    for rel in targets:
        page = pages[rel]
        counts["pages"] += 1
        if page.menu is None or not page.match:
            continue
        try:
            if page.auto:
                text = page.with_menu(merged(page.menu, rules.derive(rel)))
            else:
                # A menu the person keeps to themselves: nothing added or taken out, but a
                # title written as its key, which is all the site reads.
                text = page.with_menu([key_of(v) or v for v in page.menu])
            if text is None:
                continue
            write_page(rel, text)
        except Exception as e:   # one page that cannot be labelled never stops the others
            log(f"{rel}: not labelled ({type(e).__name__}: {e})")
            continue
        pages[rel] = Page(rel, text)
        counts["changed"] += 1
    return counts


def apply_answers(pages, answers):
    """Paths for pages, from Claude's answer: added to each page's menu like the rules'
    (its own entries first). Returns how many pages changed."""
    changed = 0
    for rel, paths in answers.items():
        page = read_page(rel) if rel in pages else None
        if page is None or not page.auto or page.menu is None or not paths:
            continue
        text = page.with_menu(merged(page.menu, paths))
        if text is None:
            continue
        try:
            write_page(rel, text)
        except OSError as e:
            log(f"{rel}: not written ({e})")
            continue
        pages[rel] = Page(rel, text)
        changed += 1
    return changed


# ======================================================================= state ===
def read_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f if line.strip()]
    except OSError:
        return []


def write_lines(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
    os.replace(tmp, path)


def add_lines(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))


def failed_recently():
    """Whether Claude could not run for the menu less than RETRY_SECONDS ago."""
    try:
        with open(ASK_FAILED, encoding="utf-8") as f:
            stamp = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    return abs(time.time() - stamp) < RETRY_SECONDS   # one far ahead (a clock set back) is long ago


def reads_with_claude(cfg):
    return (cfg.get("engine") or "claude") != "local"


# ================================================================= Claude ========
ASK_SYSTEM = ("You sort the pages of a small business's internal wiki into the sections of its menu. "
              "Judge only from what each page's title and first lines say. Choose the category a person "
              "would look in first; leave a page's list empty when none fits.")


def first_lines(page, limit=SUMMARY_CHARS):
    """The start of what a page says: its summary points, or else its first lines of text
    (no headings, markers, quotes or tables)."""
    body = page.body
    m = re.search(r"^## (?:Summary|Overview)[ \t]*\r?\n(.*?)(?=^## |^<!-- wiki-engine|\Z)", body, re.M | re.S)
    text = m.group(1) if m and m.group(1).strip() else body
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "<!--", ">", "|", "---", "```")):
            continue
        keep.append(re.sub(r"^[-*]\s+", "", s))
        if sum(len(k) for k in keep) >= limit:
            break
    plain = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", " ".join(keep))
    plain = re.sub(r"\\([\\`*_\[\]()!#|~@%:.])", r"\1", plain)
    return clean_line(plain, limit)


def page_line(n, page):
    folder = page.rel.split("/")[1]
    kind = "topic" if kind_of(page.rel) == "topic" else (page.tags[0] if page.tags else "document")
    gist = first_lines(page)
    return f"{n}. [{folder}] ({clean_line(kind, 30)}) {clean_line(page.title, 160)}" + (f" — {gist}" if gist else "")


def ask_schema():
    return {"type": "object", "additionalProperties": False, "required": ["pages"], "properties": {
        "pages": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["n", "menu"], "properties": {
                "n": {"type": "integer"},
                "menu": {"type": "array", "maxItems": 2, "items": {"type": "string", "enum": PATHS}}}}}}}


def ask_question(chunk):
    return ("The wiki's menu (each value is \"section/category\"):\n" + listing(compact=False)
            + "\n\nFor each page below, choose the one or two categories it belongs in, most fitting first, "
              "or none. A page about one document goes where that document would be filed; a topic page goes "
              "where its subject belongs. Answer with each page's number.\n\nPages:\n"
            + "\n".join(page_line(n, p) for n, p in enumerate(chunk, 1)))


def make_claude(cfg, cap):
    """Claude as the fast reader uses it (scripts/wiki_claude.py, the model fast reading
    chose), one call at a time."""
    import wiki_claude
    fast = cfg.get("fast") if isinstance(cfg.get("fast"), dict) else {}
    chosen = str(fast.get("model") or "sonnet")
    return wiki_claude.Claude(model="" if chosen == "default" else chosen, budget=min(cap, 2.5), timeout=ASK_SECONDS)


def uploads_waiting():
    """Whether a document or packet waits in the queue, exactly as the runner lists it
    (list_pending: regular files, never a link, outside folders named with a dot, by its
    rule for names it never processes), so a pass with nothing else to do makes way for
    it, and a file the runner never lists cannot hold the pass up for ever."""
    import wiki_events
    for folder in ("raw/_intake", "raw/inbox"):
        for dirpath, dirs, files in os.walk(os.path.join(WIKI_DIR, folder)):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                path = os.path.join(dirpath, f)
                if not wiki_events.ignored_name(f) and os.path.isfile(path) and not os.path.islink(path):
                    return True
    return False


def read_tries():
    tries = {}
    for line in read_lines(ASK_TRIES):
        n, _, rel = line.partition("\t")
        if rel and n.isdigit():
            tries[rel] = int(n)
    return tries


def write_tries(tries):
    write_lines(ASK_TRIES, [f"{n}\t{rel}" for rel, n in sorted(tries.items())])


def back_off(why):
    log(f"{why}; trying again in {RETRY_SECONDS // 3600} hours")
    write_lines(ASK_FAILED, [f"{int(time.time())} {clean_line(why, 200)}"])


def ask(limit=ASK_LIMIT, claude=None, yield_to_uploads=False):
    """Claude places the pages the rules could not, ASK_CHUNK a call, until limit pages
    or the spending cap per batch. The rules run first, so Claude is only asked what
    they cannot place. Every page of an answered call is recorded, placed or not, so none
    is asked twice. A call that gets no answer (Claude unable to run, a refusal, a
    timeout) stops the pass for 6 hours and records nothing, except that a page left
    unanswered in ASK_TRIES_MAX passes is given up (it stays for a person to place), so
    one call that always fails cannot hold up the rest for ever. Claude unable to run at
    all (offline, signed out, a usage limit) counts no try: it says nothing about the
    pages. yield_to_uploads: stop
    between calls when a document or packet arrives (the run with nothing else to do).
    Returns the number of pages placed."""
    cfg = config()
    if not reads_with_claude(cfg) or not PATHS:
        return 0
    import wiki_claude
    import wiki_settings
    pages = scan()
    label(pages=pages)
    todo = unplaced(pages, set(read_lines(ASKED)))
    if not todo:
        write_lines(UNPLACED, [])
    todo = todo[:max(0, int(limit))]
    if not todo:
        return 0
    cap = float(wiki_settings.effective(cfg)["maxSpendPerBatchUsd"])
    claude = claude or make_claude(cfg, cap)
    tries = read_tries()
    placed, done = 0, 0
    log(f"asking Claude where {len(todo)} page(s) the rules could not place belong")
    try:
        for i in range(0, len(todo), ASK_CHUNK):
            if claude.spent() >= cap:
                log(f"stopped at the spending cap (${cap:g}); the other pages are asked on a later run")
                break
            if yield_to_uploads and uploads_waiting():
                log("stopped for the documents just uploaded; the other pages are asked on a later run")
                break
            chunk = [pages[rel] for rel in todo[i:i + ASK_CHUNK]]
            answer, why = None, ""
            for attempt in range(2):
                try:
                    answer = claude.ask(ASK_SYSTEM, ask_question(chunk), ask_schema())
                    break
                except wiki_claude.ClaudeUnavailable as e:
                    back_off(f"Claude could not run ({e})")
                    return placed
                except Exception as e:
                    why = f"pages {i + 1} to {i + len(chunk)} got no answer ({e})"
                    log(why + ("; asking once more" if not attempt else ""))
            if answer is None:
                given_up = []
                for p in chunk:
                    tries[p.rel] = tries.get(p.rel, 0) + 1
                    if tries[p.rel] >= ASK_TRIES_MAX:
                        given_up.append(p.rel)
                        del tries[p.rel]
                if given_up:
                    add_lines(ASKED, given_up)
                    log(f"{len(given_up)} page(s) went unanswered in {ASK_TRIES_MAX} passes; left for a person to place")
                write_tries(tries)
                back_off(why or "no answer")
                return placed
            answers = {}
            for item in answer.get("pages") or []:
                n = item.get("n") if isinstance(item, dict) else None
                if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(chunk):
                    paths = normalise(item.get("menu"))[:2]
                    if paths:
                        answers.setdefault(chunk[n - 1].rel, paths)
            placed += apply_answers(pages, answers)
            add_lines(ASKED, [p.rel for p in chunk])
            for p in chunk:
                tries.pop(p.rel, None)
            write_tries(tries)
            done += len(chunk)
            try:
                import wiki_events
                wiki_events.update_now(n=done, of=len(todo))
            except Exception:
                pass
        try:
            os.remove(ASK_FAILED)
        except OSError:
            pass
    finally:
        claude.stop()
        write_lines(UNPLACED, unplaced(scan(), set(read_lines(ASKED))))
    log(f"Claude placed {placed} of {done} page(s)")
    return placed


def pending():
    """Whether ask has something to do now: pages the rules could not place that Claude
    was never asked about, a wiki that reads with Claude, and no failure in the last 6
    hours."""
    if not PATHS or not reads_with_claude(config()) or failed_recently():
        return False
    return bool(unplaced(scan(), set(read_lines(ASKED))))


# ==================================================================== command ====
def label_all(once=False):
    """The rules over the whole wiki; with once, only if engine/menu.json changed since
    the last time. Writes the pages still unplaced (menu-unplaced.txt)."""
    version = str(VERSION)
    if once and read_lines(LABELLED)[:1] == [version]:
        return 0
    if not PATHS:
        log(f"no menu to label pages with ({menu_file()} could not be read)")
        return 0
    t0 = time.time()
    remove_stale_tmp()
    pages = scan()
    counts = label(pages=pages)
    write_lines(UNPLACED, unplaced(pages, set(read_lines(ASKED))))
    if once:
        write_lines(LABELLED, [version])
    log(f"labelled {counts['changed']} of {counts['pages']} page(s) in {time.time() - t0:.1f}s"
        + (f" (menu version {version})" if once else ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sort the wiki's pages into its menu.")
    ap.add_argument("command", choices=["label", "ask", "pending"])
    ap.add_argument("--once", action="store_true", help="label: only when engine/menu.json changed")
    ap.add_argument("--limit", type=int, default=ASK_LIMIT, help="ask: at most this many pages")
    ap.add_argument("--yield", dest="yield_", action="store_true",
                    help="ask: stop between calls when a document or packet arrives")
    a = ap.parse_args(argv)
    # The runner's watchdog stops a pass with SIGTERM: unwind normally, so the Claude call
    # still running is stopped too.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        import wiki_netguard
        wiki_netguard.install()
    except Exception:
        pass
    if a.command == "pending":
        return 0 if pending() else 1
    try:
        if a.command == "label":
            return label_all(once=a.once)
        ask(limit=a.limit, yield_to_uploads=a.yield_)
        return 0
    except Exception as e:   # the menu never stops a run: it is tried again on the next one
        log(f"{a.command} failed: {type(e).__name__}: {e}")
        print(f"wiki_menu {a.command} failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
