#!/usr/bin/env python3
"""local_facts.py — figures, grounding and the claims store for the local engine.

The model on this Mac reads documents; this module decides what of its answer can be
kept and remembers it:

  canonical forms   one spelling for dates, amounts, durations and percentages, so "RM
                    97,000.00", "97000" and "RM 97,000" are the same figure, and "15 April
                    2026" is 2026-04-15
  grounding         a figure is kept only if the document contains it; a sentence the model
                    writes is kept only if every number in it is in what it was given
  candidates        what a document visibly holds (company names, people with titles,
                    amounts, dates, durations), found by pattern, so figures and parties the
                    model skipped can be asked about once more
  claims store      archive/claims.jsonl, one line per figure recorded on a page: which
                    page, what (attribute and qualifier, and the other party for terms
                    between two parties), the value, its source and the document's date.
                    Append-only and committed with each run, so `git revert` undoes it. A
                    standing term (payment terms, a price, a fee, a notice period, an
                    address) that a later source states differently is a contradiction,
                    found by code, not by asking the model.

Standard library only.
"""
import json
import os
import re
import unicodedata

ATTRIBUTES = [
    "payment_terms", "unit_price", "fee", "rent", "deposit", "credit_limit", "total_amount", "amount",
    "quantity", "percentage", "rate", "tax", "revenue", "budget", "salary",
    "date", "start_date", "end_date", "due_date", "deadline", "contract_term", "notice_period",
    "date_of_birth", "address", "phone", "email", "delivery_terms", "warranty", "headcount", "reference", "other",
]
LABELS = {"payment_terms": "Payment terms", "unit_price": "Unit price", "credit_limit": "Credit limit",
          "total_amount": "Total amount", "start_date": "Start date", "end_date": "End date",
          "due_date": "Due date", "contract_term": "Contract term", "notice_period": "Notice period",
          "delivery_terms": "Delivery terms", "salary": "Salary", "date_of_birth": "Date of birth",
          "phone": "Phone", "email": "Email"}
# Terms that stay true until something changes them: a later source that states one
# differently is a contradiction to look at. (A salary is not one: a raise is no
# contradiction.)
COMPARED = {"payment_terms", "unit_price", "fee", "rent", "deposit", "credit_limit", "contract_term",
            "notice_period", "address", "delivery_terms", "warranty", "date_of_birth"}
# Shown as one fact, its current value with the one before: the standing terms, and a
# salary (a raise shows as the new salary with the old one, and never as a contradiction).
GROUPED = COMPARED | {"salary"}
# One value per pair of parties, however it is described ("30 days from invoice date" and
# "invoice payment" are the same payment term).
SINGLE = {"payment_terms", "credit_limit"}
# What a company, person or product page holds: standing terms and figures about it, and a
# person's own details (salary, date of birth, phone, email: an employee's page keeps them
# all). One-off amounts, dates and references of a transaction stay on its document's
# summary page.
STANDING = COMPARED | {"unit_price", "rate", "percentage", "revenue", "budget", "headcount", "start_date",
                       "end_date", "contract_term", "tax", "salary", "date_of_birth", "phone", "email"}
# Terms between two parties: recorded on both parties' pages, each naming the other.
RELATIONAL = COMPARED - {"address", "date_of_birth"}
DURATION = {"payment_terms", "contract_term", "notice_period", "warranty"}
MONEY = {"unit_price", "fee", "rent", "deposit", "credit_limit", "total_amount", "amount", "tax", "revenue",
         "budget", "salary"}

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
_MON = {m: i for i, m in enumerate(MONTHS, 1)}
_MON.update({m[:3]: i for i, m in enumerate(MONTHS, 1)})
_MON["sept"] = 9
_MNAMES = "|".join(sorted(_MON, key=len, reverse=True))


def label_of(attribute):
    return LABELS.get(attribute) or attribute.replace("_", " ").capitalize()


# ============================================================== canonical forms ====
def _iso(y, m, d):
    try:
        y, m, d = int(y), int(m), int(d)
    except ValueError:
        return None
    if not (1900 <= y <= 2200 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def dates_to_iso(t):
    """Written dates as YYYY-MM-DD: '15 April 2026', 'April 15, 2026', '15/04/2026',
    '15.04.2026', '15-Apr-2026'. Day first for numeric dates (as written in Malaysia,
    the UK and most of the world)."""
    def dmy(m):
        return _iso(m.group(3), _MON[m.group(2).lower().rstrip(".")], m.group(1)) or m.group(0)

    def mdy(m):
        return _iso(m.group(3), _MON[m.group(1).lower().rstrip(".")], m.group(2)) or m.group(0)

    def num(m):
        return _iso(m.group(3), m.group(2), m.group(1)) or m.group(0)
    t = re.sub(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s-]+({_MNAMES})\.?,?[\s-]+(\d{{4}})\b", dmy, t, flags=re.I)
    t = re.sub(rf"\b({_MNAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", mdy, t, flags=re.I)
    t = re.sub(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", num, t)
    return t


ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
CUR_GLUED = re.compile(r"(?<![\w$])(RM|MYR|USD|SGD|EUR|GBP|IDR|THB|S\$|US\$|\$|€|£)(?=\d)", re.I)
CURRENCIES = [("rm", r"\b(?:rm|myr|ringgit)\b"), ("usd", r"\b(?:usd|us\$)|(?<![a-z])\$"), ("sgd", r"\b(?:sgd|s\$)"),
              ("eur", r"\beur\b|€"), ("gbp", r"\bgbp\b|£"), ("idr", r"\bidr\b"), ("thb", r"\bthb\b")]
NUM = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d])|(?<![\w.,])\d+(?:\.\d+)?(?![\d])")
ORDINAL_Q = {"first": "q1", "second": "q2", "third": "q3", "fourth": "q4", "1st": "q1", "2nd": "q2",
             "3rd": "q3", "4th": "q4"}
SCALE = {"million": 1e6, "mil": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "k": 1e3, "thousand": 1e3}


def numval(s):
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return s
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return ("%.6f" % v).rstrip("0").rstrip(".")


def canon(t):
    """One spelling for comparing: case folded, one space, dates as ISO, numbers without
    thousands separators or trailing zeros, 'fourth quarter' as 'q4'."""
    t = unicodedata.normalize("NFKC", str(t or ""))
    t = t.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    t = t.replace("&amp;", "&").replace("\\", "")
    t = CUR_GLUED.sub(r"\1 ", t)  # "RM1,500.00" is "RM 1,500.00"
    t = dates_to_iso(t)
    keep = []

    def protect(m):
        keep.append(m.group(0))
        return f"\x00{len(keep) - 1}\x00"
    t = ISO.sub(protect, t)
    t = NUM.sub(lambda m: numval(m.group(0)), t)
    t = re.sub(r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\b",
               lambda m: ORDINAL_Q[m.group(1).lower()], t, flags=re.I)
    t = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], t)
    return " ".join(t.casefold().split())


def numbers_in(text):
    """The numbers and ISO dates a text states, in canonical form. '1.2 million' also
    counts as 1200000, and 'Q4' as 4."""
    c = canon(text)
    dates = set(ISO.findall(c))
    rest = ISO.sub(" ", c)
    nums = set(re.findall(r"\d+(?:\.\d+)?", rest))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(million|mil|billion|bn|thousand|k|m)\b", rest):
        nums.add(numval(str(float(m.group(1)) * SCALE[m.group(2)])))
    return dates, nums


def ungrounded_numbers(text, source_numbers):
    """Numbers and dates in text that source_numbers (a numbers_in() pair) does not hold.
    Single digits are allowed only when the source has them too, like everything else."""
    dates, nums = numbers_in(text)
    sd, sn = source_numbers
    bad = [d for d in dates if d not in sd]
    for n in nums:
        if n in sn:
            continue
        # A year inside an ISO date of the source ('2026') is in the source.
        if re.fullmatch(r"\d{4}", n) and any(d.startswith(n) for d in sd):
            continue
        # '2026-04' style month references, and day or month parts of an ISO source date.
        bad.append(n)
    return bad


WORD = re.compile(r"[^\W_]{2,}", re.U)
STOP = {"the", "a", "an", "of", "for", "per", "and", "or", "to", "in", "on", "at", "by", "with", "from",
        "each", "every", "its", "is", "are", "be", "this", "that", "as"}


def grounded(value, source_canon, source_numbers):
    """A value the document actually contains: every number and date in it is in the
    document, and a value without numbers appears in it word for word (most words of a
    long phrase)."""
    v = canon(value)
    if not v:
        return False
    if re.search(r"\d", v):
        return not ungrounded_numbers(value, source_numbers)
    if v in source_canon:
        return True
    words = [w for w in WORD.findall(v) if w not in STOP]
    if not words:
        return False
    have = sum(1 for w in words if re.search(rf"(?<![^\W_]){re.escape(w)}(?![^\W_])", source_canon))
    return have == len(words) if len(words) <= 3 else have >= 0.8 * len(words)


# =================================================================== comparing ====
DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\)?\s*(?:\(\s*\d+\s*\)\s*)?(working days?|business days?|days?|weeks?|months?|"
                         r"years?|yrs?|mths?)", re.I)
NET_RE = re.compile(r"\bnet\s*(\d+)\b", re.I)


def norm_value(attribute, value):
    """The part of a value that decides whether two values are the same."""
    c = canon(value)
    if attribute == "address":
        digits = sorted(set(re.findall(r"\d+", c)))
        return " ".join(digits) if digits else " ".join(w for w in WORD.findall(c) if w not in STOP)
    if attribute in DURATION:
        m = DURATION_RE.search(c)
        if m:
            unit = m.group(2).lower()
            unit = ("working day" if unit.startswith(("working", "business")) else
                    "day" if unit.startswith("day") else "week" if unit.startswith("week") else
                    "month" if unit.startswith(("month", "mth")) else "year")
            return f"{m.group(1)} {unit}"
        m = NET_RE.search(c)
        if m:
            return f"{m.group(1)} day"
    if attribute in MONEY:
        rest = ISO.sub(" ", c)
        m = re.search(r"(\d+(?:\.\d+)?)\s*(million|mil|billion|bn|thousand|k|m)?\b", rest)
        if m:
            amount = numval(str(float(m.group(1)) * SCALE[m.group(2)])) if m.group(2) else m.group(1)
            cur = next((code for code, rx in CURRENCIES if re.search(rx, rest)), "")
            return f"{amount}|{cur}"
    if attribute == "percentage" or attribute == "rate":
        m = re.search(r"\d+(?:\.\d+)?", c)
        if m:
            return m.group(0) + "%"
    return c


def same_norm(a, b):
    """Two normalised values that say the same: equal, or the same amount where only one
    of them names its currency."""
    a, b = a or "", b or ""
    if a == b:
        return True
    if "|" in a and "|" in b:
        (na, ca), (nb, cb) = a.split("|", 1), b.split("|", 1)
        return na == nb and (not ca or not cb or ca == cb)
    return False


DATES = {"date", "start_date", "end_date", "due_date", "deadline", "date_of_birth"}
PAY_WORDS = re.compile(r"\b(cod|cash|upfront|advance|immediate|immediately|on delivery|on receipt|on demand)\b", re.I)


def fits(attribute, value):
    """Whether a value is the kind of thing its label says: an amount has a number, a
    period has a number and a unit, a date names a day, month or year. A value that does
    not fit ("June" as a fee) is kept, labelled as other."""
    c = canon(value)
    has_num = re.search(r"\d", c) is not None
    if attribute in MONEY:
        # An amount, not a reference number ("Q-2026-011") or a date.
        return bool(re.search(r"(?<![\w-])\d[\d,]*(?:\.\d+)?(?![\w-])", c)) and not ISO.search(c)
    if attribute in ("quantity", "percentage", "rate", "headcount"):
        return has_num
    if attribute in DURATION:
        return bool(DURATION_RE.search(c) or NET_RE.search(c) or (attribute == "payment_terms" and PAY_WORDS.search(c)))
    if attribute in DATES:
        return bool(ISO.search(c) or re.search(rf"\b(?:{_MNAMES})\b|\b(?:19|20)\d\d\b|\bq[1-4]\b", c)
                    or (attribute == "deadline" and DURATION_RE.search(c)))
    return True


TERM_WORDS = [
    ("payment_terms", r"payment terms?|pay(?:able|ment)? within|credit terms?|due within|\bnet \d"),
    ("notice_period", r"\bnotice\b"),
    ("credit_limit", r"credit limit"),
    ("deposit", r"\bdeposit\b"),
    ("rent", r"\brent(?:al)?\b"),
    ("unit_price", r"\bprice\b|\bper (?:kg|unit|piece|item|pax|litre|box|carton)\b|unit cost"),
    ("fee", r"\bfees?\b"),
    ("warranty", r"\bwarranty|\bguarantee"),
    ("contract_term", r"\bterm of\b|\bruns? for\b|\bduration\b|for a period of"),
]


def standing_terms(text):
    """Standing terms a short text states in so many words ("Payment terms are 30 days"):
    [(attribute, value, sentence)], read by pattern. Used when the model answers nothing
    about a page a person wrote, so a later document that changes one is still noticed."""
    out = []
    for c in figure_candidates(text):
        ctx = c["context"]
        at = ctx.find(c["value"])
        if at < 0:
            continue
        start = max(ctx.rfind(".", 0, at), ctx.rfind(";", 0, at)) + 1
        stops = [i for i in (ctx.find(". ", at), ctx.find(";", at)) if i != -1]
        sentence = ctx[start:min(stops) if stops else len(ctx)].strip(" -.")  # the clause it is in
        for attribute, rx in TERM_WORDS:
            if re.search(rx, sentence, re.I) and fits(attribute, c["value"]):
                out.append((attribute, c["value"], sentence))
                break
    return out


# A link that changes who owns what is kept only where the document speaks of ownership: a
# model reads a supplier the business buys from as one it owns.
RELATION_CUES = {
    "owner_of": r"\bown(?:s|ed|er|ers|ership)\b|\bsubsidiar|\bholding compan|\bsharehold|\bshares? in\b"
                r"|\bparent compan|\bproprietor|\bstake\b|\bacquir",
}
RELATION_CUES["subsidiary_of"] = RELATION_CUES["owner_of"]


def relation_stated(relation, text):
    rx = RELATION_CUES.get(relation)
    return not rx or bool(re.search(rx, text or "", re.I))


GENERIC = STOP | {"fee", "fees", "price", "prices", "rate", "rates", "cost", "costs", "charge", "charges",
                  "term", "terms", "annual", "annually", "yearly", "monthly", "year", "month", "ya",
                  "payment", "payments", "amount", "total", "agreed", "current", "new", "standard",
                  "service", "services", "per", "kg", "unit", "units", "rm", "myr", "usd", "sgd",
                  "delivery", "deliveries", "invoice", "invoices", "order", "ordered"} | set(_MON)


def qual_tokens(q):
    """The words of a qualifier that say what it is for. Words like "fee" or "April" are
    left out unless they are all it says ("delivery" alone is a delivery fee)."""
    words = [w for w in WORD.findall(canon(q)) if w not in STOP and not re.fullmatch(r"(?:19|20)\d\d", w)]
    return {w for w in words if w not in GENERIC} or set(words)


def qual_score(a, b):
    """How well two qualifiers name the same thing: 0.5 when either says nothing (it
    could be anything); 1 when one's words are all in the other ("coffee beans" and
    "washed arabica coffee beans"); else the share of all their words they have in
    common ("arabica coffee beans" and "robusta coffee beans" are different things)."""
    ta, tb = qual_tokens(a), qual_tokens(b)
    if not ta or not tb:
        return 0.5
    if ta <= tb or tb <= ta:
        return 1.0
    return len(ta & tb) / len(ta | tb)


# ================================================================== candidates ====
COMPANY_SUFFIX = (r"Sdn\.?\s*Bhd\.?|Berhad|Bhd\.?|Pte\.?\s*Ltd\.?|Ltd\.?|Limited|LLC|LLP|PLC|Inc\.?|Corp\.?|"
                  r"Corporation|GmbH|Enterprise|Enterprises|Trading|Holdings|Services|Resources")
COMPANY_RE = re.compile(rf"\b((?:[A-Z][\w'&.-]*|&)(?:[ \t]+(?:[A-Z][\w'&.-]*|&|and|of)){{0,5}}?"
                        rf"(?:[ \t]+(?i:{COMPANY_SUFFIX}))+)(?![\w])")
TITLES = (r"Director|Managing Director|Manager|Finance Manager|General Manager|Partner|Officer|"
          r"Chief Executive Officer|Chief Financial Officer|CEO|CFO|COO|Chairman|Chairperson|Chair|"
          r"Secretary|Company Secretary|Accountant|Executive|Engineer|Head of [A-Z]\w+|President|Founder|"
          r"Owner|Proprietor|Supervisor|Consultant|Auditor|Lawyer|Advocate|Clerk|Assistant")
NAME = r"[A-Z][a-z'’-]+(?:\s+(?:bin|binti|bte|a/l|a/p|van|de|[A-Z][a-z'’-]+)){1,3}"
PERSON_RES = [
    re.compile(rf"\b(?:Mr|Ms|Mrs|Mdm|Madam|Miss|Dr|Encik|En|Puan|Pn|Cik|Dato'?|Datin|Datuk|Tan Sri|Puan Sri)\.?\s+({NAME}|[A-Z][a-z'’-]+)"),
    re.compile(rf"\b({NAME}),?\s+\(?(?:the\s+)?(?:{TITLES})\b"),
    re.compile(rf"\b(?:Prepared|Approved|Reviewed|Checked|Signed|Authorised|Authorized|Accepted|Attention|Attn|"
               rf"Contact|Present|Chair|Owner)\b[^:\n]{{0,40}}:\s*({NAME})"),
    re.compile(rf"\b(?:[Bb]y|[Aa]ttention|[Aa]ttn\.?|[Cc]ontact)\s+(?:(?:Mr|Ms|Mrs|Dr)\.?\s+)?({NAME}),\s+(?:{TITLES})"),
]
CURRENCY = r"(?:RM|MYR|USD|US\$|SGD|S\$|EUR|GBP|IDR|THB|\$|€|£)"
FIGURE_RES = [
    ("money", re.compile(rf"{CURRENCY}\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|mil|bn|billion|k)\b)?", re.I)),
    ("percentage", re.compile(r"\b\d+(?:\.\d+)?\s?%")),
    ("money", re.compile(r"(?<![\w.,/-])\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?(?![\w.,/-])|(?<![\w.,/-])\d{2,}\.\d{1,2}(?![\w.,/%-])")),
    ("duration", re.compile(r"\b\d+\s*(?:\(\s*\d+\s*\)\s*)?(?:working days?|business days?|days?|weeks?|months?|years?)\b", re.I)),
    ("quantity", re.compile(r"\b\d[\d,]*(?:\.\d+)?\s?(?:kg|g|tonnes?|tons?|litres?|liters?|ml|pax|units?|pcs|pieces|"
                            r"boxes|cartons|bags|sq\.?\s?ft|sqft|m2|hours?|hrs|staff|outlets?)\b", re.I)),
    ("date", re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MNAMES})\.?,?\s+\d{{4}}\b|\b(?:{_MNAMES})\.?\s+\d{{1,2}},?\s+\d{{4}}\b|"
                        r"\b\d{1,2}/\d{1,2}/\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", re.I)),
]


def _dedupe(items):
    """First spelling of each name, but 'Northwind Trading Sdn Bhd' over a letterhead's
    'NORTHWIND TRADING SDN BHD'."""
    out, seen = [], {}
    for x in items:
        k = canon(x)
        if not k:
            continue
        if k not in seen:
            seen[k] = len(out)
            out.append(x)
        elif out[seen[k]].isupper() and not x.isupper():
            out[seen[k]] = x
    return out


HONORIFIC_RE = re.compile(r"^(?:Mr|Ms|Mrs|Mdm|Madam|Miss|Dr|Encik|En|Puan|Pn|Cik)\.?\s+")
GENERIC_NAME_WORDS = {"tax", "the", "our", "your", "general", "professional", "financial", "management", "trading",
                      "services", "service", "and", "&", "of", "agreement", "letter", "engagement"}


def party_candidates(text):
    """Company names (with a legal or trade suffix) and people (by title, honorific or a
    'Prepared by: ...' line) that the text visibly names."""
    firms, persons = [], []
    for m in COMPANY_RE.finditer(text):
        name = " ".join(m.group(1).split())
        name = re.sub(r"^(?:The|And|Of|To|From|Between|Bill|Pay|Paid|For|Signed|Accepted)\s+", "", name)
        core = re.sub(rf"(?:[ \t]+(?i:{COMPANY_SUFFIX}))+$", "", " " + name).split()
        if core and not all(w.casefold() in GENERIC_NAME_WORDS for w in core):
            firms.append(name)
    for rx in PERSON_RES:
        for m in rx.finditer(text):
            name = HONORIFIC_RE.sub("", " ".join(m.group(1).split()))
            if len(name.split()) >= 2 and not re.search(COMPANY_SUFFIX, name) and len(name) <= 60:
                persons.append(name)
    firms = _dedupe(firms)
    # A person's name inside a company name ("Tan" in "Tan & Partners") is not a person.
    persons = [p for p in _dedupe(persons) if not any(canon(p) == canon(c) for c in firms)]
    return firms, persons


def figure_candidates(text, limit=60):
    """Amounts, percentages, durations, quantities and dates the text visibly states,
    each with a little context, in document order."""
    found = []
    for kind, rx in FIGURE_RES:
        for m in rx.finditer(text):
            found.append((m.start(), kind, " ".join(m.group(0).split())))
    found.sort(key=lambda x: (x[0], -len(x[2])))
    spans, kept = [], []
    for pos, kind, val in found:  # "2,000" inside "2,000 kg" is the same figure
        end = pos + len(val)
        if any(a <= pos and end <= b for a, b in spans):
            continue
        spans.append((pos, end))
        kept.append((pos, kind, val))
    out, seen = [], set()
    for pos, kind, val in kept:
        k = norm_value("amount" if kind == "money" else "date" if kind == "date" else kind, val)
        if (kind, k) in seen:
            continue
        seen.add((kind, k))
        line_start = text.rfind("\n", 0, pos) + 1
        line_end = text.find("\n", pos)
        ctx = " ".join(text[max(line_start, pos - 80):(line_end if line_end != -1 else len(text))][:160].split())
        out.append({"kind": kind, "value": val, "context": ctx})
        if len(out) >= limit:
            break
    return out


def covered(candidate_value, values_canon):
    """Whether a figure candidate is already among the extracted values."""
    d, n = numbers_in(candidate_value)
    if not d and not n:
        return True
    return all(x in values_canon[0] for x in d) and all(x in values_canon[1] for x in n)


# ================================================================ claims store ====
class Claims:
    """archive/claims.jsonl: what figures each page holds, from which source.

    A record: {"page", "attribute", "qualifier", "counterparty", "value", "norm", "source",
    "source_title", "doc_date", "recorded", "seeded"}. Records are never changed or
    removed; the current value of a fact is its newest record by document date. A page
    whose text was read for figures (a page a person wrote) gets one record with
    attribute "_seeded", so it is read only once."""

    def __init__(self, path):
        self.path = path
        self.items = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue  # a hand-edited or cut-off line is skipped, not fatal
                    if isinstance(r, dict) and r.get("page") and r.get("attribute"):
                        self.items.append(r)
        except OSError:
            pass
        self.pending = []

    def seeded(self, page):
        """Whether the page's figures are known: read from its text once, or recorded
        from documents."""
        return any(r["page"] == page and (r["attribute"] == "_seeded" or not r["attribute"].startswith("_"))
                   for r in self.items + self.pending)

    def for_page(self, page):
        """The page's figures (records about relationships, documents and topic points,
        whose attribute starts with "_", are kept apart)."""
        return [r for r in self.items + self.pending if r["page"] == page and not r["attribute"].startswith("_")]

    def records(self, attribute, page=None, other=None):
        """Records of one kind: "_rel" (page = subject, counterparty = object, qualifier =
        relation), "_doc" (a document that names the page, value = its role there),
        "_point" (a point a document makes for a topic page)."""
        return [r for r in self.items + self.pending if r["attribute"] == attribute
                and (page is None or r["page"] == page or (other and r.get("counterparty") == page))]

    def pages(self):
        return {r["page"] for r in self.items + self.pending}

    def add(self, rec):
        """Queue a record (written by save()); the same figure from the same source twice
        (a retried document) is recorded once."""
        def key(r):
            return (r["page"], r["attribute"], canon(r.get("qualifier", "")), r.get("counterparty", ""),
                    r.get("norm", ""), r.get("source", ""), canon(r.get("value", "")))
        k = key(rec)
        if any(key(r) == k for r in self.items + self.pending):
            return False
        self.pending.append(rec)
        return True

    def save(self):
        if not self.pending:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            for r in self.pending:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        self.items += self.pending
        self.pending = []

    @staticmethod
    def order(r):
        return (r.get("doc_date") or "0000-00-00", r.get("recorded") or "")

    @staticmethod
    def compatible(a, b):
        """Two records about the same fact of the same page."""
        if a["page"] != b["page"] or a["attribute"] != b["attribute"]:
            return 0
        ca, cb = a.get("counterparty") or "", b.get("counterparty") or ""
        if ca and cb and ca != cb:
            return 0
        if a["attribute"] in SINGLE:
            return 1.0
        s = qual_score(a.get("qualifier", ""), b.get("qualifier", ""))
        return s if s >= 0.5 else 0

    @classmethod
    def fit(cls, r, group):
        """How well a record fits a group: judged against the members that say what they
        are for, when there are any (an unqualified member must not join unrelated facts)."""
        named = [x for x in group if qual_tokens(x.get("qualifier", ""))] or group
        return max(cls.compatible(r, x) for x in named)

    @classmethod
    def best_group(cls, r, groups):
        scored = [(cls.fit(r, g), g) for g in groups]
        top = max((s for s, _ in scored), default=0)
        if not top:
            return None
        best = [g for s, g in scored if s == top]
        # A figure that says nothing about what it is for could be any of several facts of
        # its kind ('RM 150' next to two fees): it is none of them.
        return best[0] if len(best) == 1 else None

    def facts(self, page):
        """The page's facts: groups of records about the same thing, newest first within
        each group; groups ordered by attribute, newest first within an attribute."""
        groups = []
        for r in sorted(self.for_page(page), key=self.order):
            # Two different figures of one document are two facts (a subtotal and a total),
            # never a value and the value it replaced.
            g = self.best_group(r, [x for x in groups if x[0]["attribute"] in GROUPED and not any(
                y.get("source") == r.get("source") and not same_norm(y.get("norm"), r.get("norm"))
                for y in x)]) if r["attribute"] in GROUPED else None
            if g is not None:
                g.append(r)
            else:
                groups.append([r])
        for g in groups:
            g.sort(key=self.order, reverse=True)
        groups.sort(key=lambda g: self.order(g[0]), reverse=True)
        groups.sort(key=lambda g: ATTRIBUTES.index(g[0]["attribute"]) if g[0]["attribute"] in ATTRIBUTES else 99)
        return groups

    def conflict(self, new):
        """The current value of the same standing fact from another source, when it differs
        from the new record's; else None. Records of the new record's own source are left
        out (one document can state a figure twice)."""
        if new["attribute"] not in COMPARED:
            return None
        groups = [[r for r in g if r is not new and r.get("source") != new.get("source")]
                  for g in self.facts(new["page"])]
        g = self.best_group(new, [g for g in groups if g])
        if g and not same_norm(g[0].get("norm"), new.get("norm")):
            return g[0]
        return None
