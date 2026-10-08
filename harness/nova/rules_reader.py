"""The rules reader: turns a document's text into typed facts without a model.

It is the fallback when no API key is set or a model call fails, and a second opinion on what the model read (when
both agree on a number, the extraction confidence goes up). It is written to read by meaning where it can: charge
lines are recognised by the charge labels the contracts themselves list (Annex E), and the qty / rate / amount on a
line are told apart by checking the arithmetic (qty x rate = amount, base x pct = amount), not by column position.
"""
from __future__ import annotations

import re
from decimal import Decimal

from .facts import DEFAULT_ALIASES
from .util import AWB_RE, D, LANE_RE, norm_label, parse_date, r2

NUM_RE = re.compile(r"(?<![\w/.-])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w/])|(?<![\w/.-])-?\d+(?:\.\d+)?(?![\w/])")
QTY_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s?(kg|kgs|shpt|shpts|shipment|shipments|days?|fee|fees|months?|pcs|"
                    r"pieces|units?|x)\b", re.I)
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?%")
UNIT_MAP = {"kg": "kg", "kgs": "kg", "shpt": "shipment", "shpts": "shipment", "shipment": "shipment",
            "shipments": "shipment", "day": "day", "days": "day", "fee": "fee", "fees": "fee", "month": "month",
            "months": "month", "pcs": "piece", "pieces": "piece", "unit": "unit", "units": "unit", "x": "unit"}
INVOICE_ID_PATTERNS = [
    r"\bINVOICE\s+(?:NO\.?\s*|NUMBER\s*)?([A-Z][A-Z0-9/\-]{4,})",
    r"Freight bill no\.?\s*([A-Z0-9][A-Z0-9/\-]{4,})",
    r"Charges advice\s*([A-Z0-9][A-Z0-9/\-]{4,})",
    r"STATEMENT OF ACCOUNT\s+([A-Z0-9][A-Z0-9/\-]{4,})",
    r"(?:Invoice|Bill|Document)\s+(?:no\.?|number|#)\s*:?\s*([A-Z0-9][A-Z0-9/\-]{4,})",
]
DATE_TOKEN = (r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2} [A-Z][a-z]{2,8} \d{4}|[A-Z][a-z]{2,8} \d{1,2}, \d{4})")


def lines_of(doc: dict) -> list[list[str]]:
    return [p.split("\n") for p in doc["pages"]]


def body_lines(doc: dict) -> list[tuple[int, str, bool]]:
    """Email body lines with their 1-based line number and whether the line is quoted/forwarded text."""
    ls = lines_of(doc)[0]
    out, in_body, quoted = [], False, False
    for i, s in enumerate(ls, 1):
        if not in_body:
            if s == "":
                in_body = True
            continue
        if s.startswith("Attachments: "):
            continue
        if re.match(r"^On .{4,80} wrote:\s*$", s) or re.match(r"^-{2,}\s*(Forwarded|Original) [Mm]essage", s) or \
                re.match(r"^Begin forwarded message", s):
            quoted = True
            out.append((i, s, True))
            continue
        out.append((i, s, quoted or s.lstrip().startswith(">")))
    return out


def find(pages: list[list[str]], pattern: str, flags=0):
    """First match of a pattern on any line -> (match, page, line)."""
    rx = re.compile(pattern, flags)
    for p, ls in enumerate(pages, 1):
        for i, s in enumerate(ls, 1):
            m = rx.search(s)
            if m:
                return m, p, i
    return None, None, None


def _src(p, i, j=None):
    return [p, i] if j is None or j == i else [p, i, j]


# ---- contracts ---------------------------------------------------------------------------------------------------
CLAUSE_KEYS = [
    ("qualifying spend", "qs"), ("pickup date", "pickup_def"), ("contract year", "contract_year"),
    ("effective date", "effective"), ("initial term", "term"), ("auto-renewal", "renewal"), ("renewal", "renewal"),
    ("extension of due date", "extension"), ("extension", "term_extension"), ("rates", "rates"),
    ("pricing date", "pricing_date"), ("chargeable weight", "chargeable_weight"), ("minimum charge", "band"),
    ("rate notices", "rate_notice"), ("fuel surcharge", "fuel"), ("security surcharge", "security"),
    ("dangerous goods fee", "dg"), ("dry ice", "dry_ice"), ("mixed shipments", "mixed"), ("storage", "storage"),
    ("delivery", "delivery"), ("customs holds", "customs"), ("volume rebate", "rebate"), ("thresholds", "thresholds"),
    ("rebate credit", "rebate_credit"), ("handling margin", "margin"), ("margin step-down", "margin_step"),
    ("annual improvement credit", "improvement"), ("payment terms", "payment"), ("late fee", "late_fee"),
    ("compounding", "compounding"), ("disputed amounts", "disputed"),
    ("waiver authority", "waiver_authority"), ("scope of waiver", "waiver_scope"),
    ("conditional waiver", "waiver_condition"), ("disputes", "disputes"), ("dispute notice", "dispute_notice"),
    ("unranked instruments", "unranked"), ("precedence", "precedence"), ("correspondence", "correspondence"),
    ("quoted text", "quoted"),
]
CLAUSE_RE = re.compile(r"^(\d{1,2}\.\d{1,2})\s+([A-Z][A-Za-z ,'\-/()]{2,60}?)\.\s")


def _clauses(pages):
    """Numbered clauses 'n.m Title. text...' with their line spans (until the next clause, heading or page end)."""
    out = {}
    for p, ls in enumerate(pages, 1):
        cur = None
        for i, s in enumerate(ls, 1):
            m = CLAUSE_RE.match(s)
            stop = m or re.match(r"^\d{1,2} [A-Z][a-z]", s) or s.startswith(("Initials:", "Annex ", "Schedule "))
            if stop and cur:
                out.setdefault(cur["title"].lower(), cur)
                cur = None
            if m:
                cur = {"no": m[1], "title": m[2].strip(), "page": p, "from": i, "to": i, "text": s}
            elif cur and s and not stop and not s.startswith(("Confidential", "Page ")):
                cur["to"] = i
                cur["text"] += " " + s
        if cur:
            out.setdefault(cur["title"].lower(), cur)
    keyed = {}
    for title, c in out.items():
        for kw, key in CLAUSE_KEYS:
            if title == kw or (title.startswith(kw) and key not in keyed):
                keyed.setdefault(key, c)
                break
    return keyed


def read_contract(doc: dict) -> dict:
    pages = lines_of(doc)
    m, _, _ = find(pages, r"Agreement No\.?\s*([A-Z0-9][A-Z0-9\-/]+)")
    cid = m[1] if m else None
    cl = _clauses(pages)
    f: dict = {"type": "contract", "contract_id": cid, "clauses": {k: _src(c["page"], c["from"], c["to"])
                                                                  for k, c in cl.items()}}
    txt = lambda k: cl[k]["text"] if k in cl else ""          # noqa: E731

    def first_date(s):
        m_ = re.search(DATE_TOKEN, s)
        return parse_date(m_[1]).isoformat() if m_ and parse_date(m_[1]) else None

    f["effective_date"] = first_date(txt("effective"))
    tm = re.search(r"(?:ending on|expires on|ends on|until)\s+" + DATE_TOKEN, txt("term"))
    f["end_date"] = parse_date(tm[1]).isoformat() if tm else first_date(txt("term"))
    ren = txt("renewal").lower()
    f["auto_renew"] = bool(ren) and "renew" in ren and not re.search(r"does not (renew|continue)|no automatic", ren)
    nd = re.search(r"(\d+)\s+days'? (?:prior |written )?notice", ren)
    f["notice_days"] = int(nd[1]) if nd and f["auto_renew"] else None
    pm = re.search(r"(\d+)\s+days", txt("payment"))
    f["payment_days"] = int(pm[1]) if pm else None
    lf = txt("late_fee")
    pct = re.search(r"(\d+(?:\.\d+)?)%\s+per month", lf)
    grace = re.search(r"more than (\d+) days", lf) or re.search(r"days_late <= (\d+)", lf)
    f["late_fee"] = {"pct": pct[1] if pct else None, "grace_days": int(grace[1]) if grace else 0,
                     "compounding": "principal plus unpaid" in txt("compounding") or "compound" in
                                    txt("compounding").lower() and "not compound" not in txt("compounding").lower()}
    sf = re.search(r"(\d+) free days", txt("storage"))
    f["storage_free_days"] = int(sf[1]) if sf else 0
    f["last_period_continues"] = "continue to apply" in txt("rates")
    f["unranked_notice_vs_amendment"] = "neither prevails" in txt("unranked").lower() or \
        "does not rank" in txt("correspondence").lower()
    # rebate
    rb = txt("rebate")
    tiers = [{"min_usd": str(D(a)), "pct": b} for a, b in re.findall(r"QS\s*>=\s*USD\s*([\d,]+(?:\.\d+)?):\s*([\d.]+)%", rb)]
    cd = re.search(r"(\d+) days after", txt("rebate_credit"))
    f["rebate"] = {"tiers": tiers, "credit_days": int(cd[1]) if cd else 45} if tiers else None
    im = txt("improvement")
    ip = re.search(r"(\d+(?:\.\d+)?)%", im)
    idays = re.search(r"(\d+) days after", im)
    f["improvement"] = ({"pct": ip[1], "due_days": int(idays[1]) if idays else 60}
                        if ip and "not applicable" not in im.lower() else None)
    qs = txt("qs").lower()
    f["qs"] = {"includes_accessorials": "accessorial" in qs, "excludes": [w for w in ("tax", "dut", "late fee", "rebate")
                                                                          if w in qs]}
    # Annex A
    periods, rates, fuel, acc, aliases, fx, sched = [], [], [], [], [], [], []
    section = None
    carrier_party = shipper_party = None
    currency = None
    for p, ls in enumerate(pages, 1):
        for i, s in enumerate(ls, 1):
            if s.startswith("Annex A"):
                section = "A"
                cm = re.search(r"\(([A-Z]{3}) per", s)
                currency = cm[1] if cm else currency
                continue
            if s.startswith("Annex B"):
                section = "B"
                continue
            if s.startswith("Annex C"):
                section = "C"
                continue
            if s.startswith("Annex D"):
                section = "D"
                continue
            if s.startswith("Annex E"):
                section = "E"
                continue
            if s.startswith("Annex F"):
                section = "F"
                continue
            if s.startswith("Schedule 1"):
                section = "S"
                continue
            if section == "A":
                pm_ = re.match(r"Period (\w+): pickups " + DATE_TOKEN + r" to " + DATE_TOKEN, s)
                if pm_:
                    periods.append({"name": pm_[1], "from": parse_date(pm_[2]).isoformat(),
                                    "to": parse_date(pm_[3]).isoformat(), "src": [p, i]})
                    continue
                row = parse_rate_row(s, [x["name"] for x in periods])
                if row:
                    row["src"] = [p, i]
                    rates.append(row)
            elif section == "C":
                m_ = re.match(r"(\d{4}-\d{2})\s+(\d+(?:\.\d+)?)%", s)
                if m_:
                    fuel.append({"month": m_[1], "pct": m_[2], "src": [p, i]})
            elif section == "D":
                m_ = re.match(r"([A-Z]{2,8})\s+(.+?)\s+([\d,]+\.\d+)\s+([A-Z]{3})\s+per\s+(.+)$", s)
                if m_:
                    unit = m_[5].lower()
                    months = None
                    mm = re.search(r"pickups in ([A-Z][a-z]+) to ([A-Z][a-z]+)", m_[5])
                    if mm:
                        from .util import MONTHS
                        a, b = MONTHS[mm[1].lower()], MONTHS[mm[2].lower()]
                        months = list(range(a, b + 1)) if a <= b else list(range(a, 13)) + list(range(1, b + 1))
                    u = ("kg" if "kg" in unit else "day" if "day" in unit else "month" if "month" in unit
                         else "shipment" if "shipment" in unit else unit.split(",")[0].strip())
                    acc.append({"code": m_[1], "name": m_[2], "fee": str(D(m_[3])), "currency": m_[4], "unit": u,
                                "months": months, "src": [p, i]})
                elif acc and s and not s.startswith(("Initials", "Confidential", "Code ", "GST")):
                    mm = re.search(r"pickups in ([A-Z][a-z]+) to ([A-Z][a-z]+)", s)
                    if mm and acc[-1]["months"] is None:
                        from .util import MONTHS
                        a, b = MONTHS[mm[1].lower()], MONTHS[mm[2].lower()]
                        acc[-1]["months"] = list(range(a, b + 1)) if a <= b else list(range(a, 13)) + list(range(1, b + 1))
            elif section == "E":
                m_ = re.match(r"Code ([A-Z]+):\s*(.+?)(?:;\s*also billed as:\s*(.+))?$", s)
                if m_:
                    labels = [m_[2].strip()] + [x.strip() for x in (m_[3] or "").split(",") if x.strip()]
                    aliases.append({"code": m_[1], "labels": labels, "src": [p, i]})
            elif section == "F":
                m_ = re.match(r"(\d{4}-\d{2})\s+(\d+(?:\.\d+)?)\s*$", s)
                if m_:
                    fx.append({"month": m_[1], "rate": m_[2], "src": [p, i]})
            elif section == "S":
                m_ = re.match(r"(.+?),\s*(.+?),\s*([A-Z0-9][A-Z0-9\-]+):\s*(.+?)\s*<([^>]+)>", s)
                if m_:
                    cats = [c.strip() for c in m_[4].split(",") if c.strip()]
                    sched.append({"name": m_[1].strip(), "title": m_[2].strip(), "party": m_[3], "categories": cats,
                                  "email": m_[5].strip().lower(), "src": [p, i]})
    for pers in sched:
        t = pers["title"].lower()
        if "key account" in t or "pricing" in t:
            carrier_party = carrier_party or pers["party"]
        if "ap manager" in t or "logistics director" in t or "procurement" in t:
            shipper_party = shipper_party or pers["party"]
    f.update({"periods": periods, "rates": rates, "fuel": fuel, "accessorials": acc, "aliases": aliases, "fx": fx,
              "schedule1": sched, "carrier_party": carrier_party, "shipper_party": shipper_party,
              "currency": currency or "USD", "src": [1, 1]})
    if cid:
        m2, p2, i2 = find(pages, re.escape(cid))
        f["src"] = [p2, i2]
    return f


BAND_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*kg|(\d+(?:\.\d+)?)\s*\+\s*kg|minimum charge", re.I)


def parse_band(text: str):
    m = BAND_RE.search(text)
    if not m:
        return None
    if m[0].lower().startswith("minimum"):
        return {"minimum": True, "band_min": None, "band_max": None}
    if m[3]:
        return {"minimum": False, "band_min": m[3], "band_max": None}
    return {"minimum": False, "band_min": m[1], "band_max": m[2]}


def parse_rate_row(s: str, period_names: list[str]):
    """'USS2-INN1 0-44.5 kg 8.98 8.94' / 'USS2-INN1 minimum charge 91.00 91.00' / 'LANE 45-99.5 kg: 5.03 USD'."""
    lm = LANE_RE.search(s)
    if not lm:
        return None
    rest = s[lm.end():]
    band = parse_band(rest)
    if not band:
        return None
    after = BAND_RE.split(rest, maxsplit=1)[-1] if False else rest[BAND_RE.search(rest).end():]
    nums = [n for n in re.findall(r"\d+(?:,\d{3})*\.\d+|\d+", after.replace(",", "")) if "." in n]
    cur = re.search(r"\b(USD|INR|EUR|GBP|SGD|AED|CNY|JPY)\b", s)
    if not nums:
        return None
    row = {"lane": lm[1], **band, "currency": cur[1] if cur else None}
    if period_names and len(nums) >= len(period_names):
        row["rates"] = {n: nums[k] for k, n in enumerate(period_names)}
    else:
        row["rate"] = nums[0]
    return row


def read_amendment(doc: dict) -> dict:
    pages = lines_of(doc)
    m, _, _ = find(pages, r"Amendment reference\s+(\S+)")
    ref = m[1] if m else None
    m, _, _ = find(pages, r"(?:Master Services Agreement[^A-Z]*\(?[A-Za-z ]*\)?\s+|Agreement\s+)([A-Z]{2,4}-[A-Z]{2}-[A-Z]{2,4}-[A-Z0-9]+)")
    cid = m[1] if m else (ref.split("/")[0] if ref else None)
    m, pe, ie = find(pages, r"Effective (?:from|date)?:?\s*" + DATE_TOKEN)
    eff = parse_date(m[1]).isoformat() if m else None
    rates, annexes, term = [], [], None
    for p, ls in enumerate(pages, 1):
        for i, s in enumerate(ls, 1):
            row = parse_rate_row(s, [])
            if row and "Annex A rev" not in s and "lane(s)" not in s:
                row["src"] = [p, i]
                rates.append(row)
    # numbered paragraphs, joined across line wraps
    for p, a, b, s in paragraphs(pages):
        am = re.search(r"(Annex [A-Z](?: rev(?:ision)? ?\d+)?(?: update)?)\s*\(attached\)\s*applies from\s+" + DATE_TOKEN +
                       r"\s+to lane\(?s?\)?\s+([A-Z0-9\-, ]+)", s)
        if am:
            annexes.append({"name": am[1], "from": parse_date(am[2]).isoformat(), "lanes": LANE_RE.findall(am[3]),
                            "attached": False, "src": [p, a, b]})
        tm = re.search(r"(?:term|Agreement) is extended (?:to|until)\s+" + DATE_TOKEN, s, re.I) or \
            re.search(r"(?:initial )?term .*?(?:ending on|expires on|ends on|until)\s+" + DATE_TOKEN, s, re.I)
        if tm:
            term = {"end_date": parse_date(tm[1]).isoformat(), "src": [p, a, b]}
    signed = sum("Signed for and on behalf" in s for ls in pages for s in ls) >= 2
    attached_names = [n.lower() for n in doc.get("meta", {}).get("attachments", [])]
    for a in annexes:
        a["attached"] = any(norm_label(a["name"]) in norm_label(n) for n in attached_names)
    return {"type": "amendment", "amendment_ref": ref, "contract_id": cid, "effective_from": eff, "signed": signed,
            "rates": rates, "annex_refs": annexes, "term": term, "src": [pe or 1, ie or 1]}


def paragraphs(pages):
    """Numbered paragraphs ('1. ...', '2. ...') with wrapped lines joined -> (page, first, last, text)."""
    out = []
    for p, ls in enumerate(pages, 1):
        cur = None
        for i, s in enumerate(ls, 1):
            if re.match(r"^\d{1,2}\.\s", s):
                if cur:
                    out.append(tuple(cur))
                cur = [p, i, i, s]
            elif cur and s and not re.match(r"^(All other terms|This Amendment|Executed|Signed|Initials|Confidential)", s) \
                    and not LANE_RE.match(s) and not s.startswith("Lane "):
                cur[2], cur[3] = i, cur[3] + " " + s
            elif cur:
                out.append(tuple(cur))
                cur = None
        if cur:
            out.append(tuple(cur))
    return out


# ---- emails --------------------------------------------------------------------------------------------------------
AUTOMATION_RE = re.compile(r"\b(AI|A\.I\.|automated|automation|auditor|audit(?:ing)? (?:bot|system|tool)|assistant|"
                           r"language model|LLM|bot|agent|system prompt|ignore (?:all |any )?(?:previous|prior))\b")


def read_email(doc: dict, labels: dict[str, str]) -> list[dict]:
    meta = doc["meta"]
    body = body_lines(doc)
    own = [(i, s) for i, s, q in body if not q]
    quoted = [(i, s) for i, s, q in body if q]
    facts: list[dict] = []
    text = " ".join(s for _, s in own)
    for i, s in own:
        if AUTOMATION_RE.search(s) and re.search(r"\b(please|must|should|do not|don't|ignore|mark|treat|approve|"
                                                 r"instruct|instruction|note to|attention)\b", s, re.I):
            facts.append({"type": "injection", "text": s, "src": [1, i]})
    # rate notice
    rows = []
    for i, s in own:
        row = parse_rate_row(s, [])
        if row:
            row["src"] = [1, i]
            rows.append(row)
    if rows:
        win = re.search(r"(?:effective|from)\s+" + DATE_TOKEN + r"\s+(?:to|until|through)\s+" + DATE_TOKEN, text)
        win1 = re.search(r"(?:effective|from)\s+(?:on\s+)?" + DATE_TOKEN, text)
        ref = re.search(r"(?:Reference|Notice ref\.?|Ref\.?):?\s*(\S+)", text)
        cid = re.search(r"\b([A-Z]{2,4}-[A-Z]{2}-[A-Z]{2,4}-[A-Z0-9]+)\b", text)
        wl = next((i for i, s in own if re.search(r"effective|from " , s)), rows[0]["src"][1])
        facts.append({"type": "rate_notice", "reference": ref[1] if ref else None,
                      "contract_id": cid[1] if cid else None,
                      "effective_from": parse_date(win[1]).isoformat() if win else (parse_date(win1[1]).isoformat() if win1 else None),
                      "effective_to": parse_date(win[2]).isoformat() if win else None,
                      "rates": rows, "src": [1, wl]})
    # waiver (own text)
    for i, s in own:
        if re.search(r"\bwaiv", s, re.I) and not re.search(r"\bno waiver\b|\bnot (?:be )?waived\b", s, re.I):
            facts.append(_waiver(s, i, labels, text, quoted=False))
    if not any(f["type"] == "waiver" for f in facts):
        for i, s in quoted:
            if re.search(r"\bwaiv", s, re.I):
                w = _waiver(s.lstrip("> ").strip(), i, labels, text, quoted=True)
                qs = next((q for _, q in quoted if re.search(r"^On .* (.+?) wrote:|^From:", q)), "")
                mm = re.search(r"^On .*?,\s*(.+?)\s+(?:<[^>]+>\s*)?wrote:", qs) or re.search(r"From:\s*(.+?)\s*<", qs)
                w["quoted_sender"] = mm[1].strip() if mm else None
                facts.append(w)
                break
    # due date extension
    for i, s in own:
        m = re.search(r"extend(?:ed)? the due date of (?:invoice )?([A-Z0-9][A-Z0-9/\-]{4,}) to " + DATE_TOKEN, s, re.I) or \
            re.search(r"(?:Invoice )?([A-Z0-9][A-Z0-9/\-]{4,}) now falls due on " + DATE_TOKEN, s, re.I) or \
            re.search(r"due date (?:of|for) (?:invoice )?([A-Z0-9][A-Z0-9/\-]{4,}) (?:is|has been) (?:extended|moved) to " + DATE_TOKEN, s, re.I)
        if m:
            facts.append({"type": "due_date_extension", "invoice_id": m[1], "new_due_date": parse_date(m[2]).isoformat(),
                          "src": [1, i]})
    # milestones
    for i, s in own:
        m = re.search(r"(?:Shipment|AWB)\s+(\S+)\s+was\s+(picked up|delivered|collected(?: by the consignee)?|"
                      r"released|arrived)\b(?:\s+(?:at|from)\s+([A-Z0-9]{3,5}))?\s+on\s+" + DATE_TOKEN, s, re.I)
        if m:
            ev = {"picked up": "PICKED_UP", "delivered": "DELIVERED", "released": "RELEASED",
                  "arrived": "ARRIVED_FACILITY"}.get(m[2].lower(), "COLLECTED_BY_CONSIGNEE")
            facts.append({"type": "shipment_event", "awb": m[1], "event": ev, "date": parse_date(m[4]).isoformat(),
                          "station": m[3], "reason": None, "src": [1, i]})
    # customs
    hold = re.search(r"(?:Shipment|AWB)\s+(\S+)\s+was placed on customs hold on\s+" + DATE_TOKEN, text)
    if hold:
        rel = re.search(r"Released on\s+" + DATE_TOKEN, text)
        rc = re.search(r"Reason code\s+([A-Z_]+)", text)
        reason = rc[1] if rc else ""
        fault = ("carrier" if re.search(r"CARRIER|carrier'?s? (error|omission)", text) else
                 "shipper" if re.search(r"SHIPPER|shipper'?s?", reason + " " + text) else "unknown")
        li = next(i for i, s in own if "customs hold" in s)
        facts.append({"type": "customs_hold", "awb": hold[1], "date": parse_date(hold[2]).isoformat(),
                      "released": parse_date(rel[1]).isoformat() if rel else None, "reason": reason,
                      "fault": fault, "src": [1, li]})
    # invoice cover
    subj = meta.get("subject", "")
    if meta.get("attachments") or re.search(r"\binvoice\b", subj, re.I):
        ids = re.findall(r"(?:Invoice|Statement|Freight bill|Charges advice|Credit note)\s+([A-Z0-9][A-Z0-9/\-]{4,})",
                         subj + " " + text, re.I)
        asks = None
        li = None
        for i, s in own:
            if re.search(r"process payment|please pay|remit|payment at your earliest|kindly (?:settle|pay)|"
                         r"is (?:now )?(?:due|overdue|outstanding)|as outstanding", s, re.I):
                asks, li = True, i
                break
        copy = None
        for i, s in own:
            if re.search(r"\bcopy\b|for your records|no action (?:is )?(?:needed|required)|already (?:been )?paid|"
                         r"\breminder only\b|do not pay", s, re.I):
                copy, li = True, li or i
        if ids:
            facts.append({"type": "invoice_cover", "invoice_ids": list(dict.fromkeys(ids)),
                          "asks_payment": bool(asks) and not copy, "copy": bool(copy),
                          "src": [1, li or 1]})
    return facts


def _waiver(s: str, i: int, labels: dict[str, str], text: str, quoted: bool) -> dict:
    awbs = AWB_RE.findall(s)
    label = None
    best = 0
    ns = norm_label(s)
    for lab in labels:
        if lab and lab in ns and len(lab) > best:
            label, best = labels[lab], len(lab)
    months = [f"{d.year:04d}-{d.month:02d}" for d in
              [parse_date(x) for x in re.findall(r"\b(?:[A-Z][a-z]+ \d{4})\b", s)] if d]
    months += re.findall(r"\b(\d{4}-\d{2})\b(?!-)", s)
    inv = [x for x in re.findall(r"\b(?:invoice|statement)\s+([A-Z0-9][A-Z0-9/\-]{4,})", s, re.I)]
    cond = re.search(r"(provided (?:that )?.+|on condition that .+|conditional (?:up)?on .+|subject to .+)", text, re.I)
    return {"type": "waiver", "charge_code": label, "charge_label": label, "awbs": awbs, "months": months,
            "invoice_ids": inv, "condition": cond[1] if cond else None, "quoted": quoted, "src": [1, i]}


# ---- invoices ------------------------------------------------------------------------------------------------------
def build_label_index(contract_facts: list[dict]) -> dict[str, str]:
    """normalised label -> code, from DEFAULT_ALIASES plus every contract's Annex E and Annex D names."""
    idx = {}
    for code, labels in DEFAULT_ALIASES.items():
        for lab in labels:
            idx[norm_label(lab)] = code
    for c in contract_facts:
        for a in c.get("aliases") or []:
            for lab in a.get("labels") or []:
                idx[norm_label(lab)] = a["code"]
        for a in c.get("accessorials") or []:
            idx[norm_label(a["name"])] = a["code"]
    return idx


def match_label(s: str, labels: dict[str, str]):
    """Longest known charge label inside the line -> (code, label_norm)."""
    ns = norm_label(s)
    best = None
    for lab, code in labels.items():
        if len(lab) >= 3 and lab in ns and (best is None or len(lab) > len(best[1])):
            best = (code, lab)
    return best


def _dayfirst(pages) -> bool:
    for ls in pages:
        for s in ls:
            for a, b, _ in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", s):
                if int(a) > 12:
                    return True
                if int(b) > 12:
                    return False
    return True


def _num(tok: str) -> Decimal:
    return D(tok.replace(",", ""))


def parse_invoice_line(s: str, labels, dayfirst=True, order_amount_first=None):
    """One charge line -> dict, or None. Recognised by its charge label; numbers sorted out by arithmetic."""
    if re.search(r"\b(subtotal|total|balance due|net charges|charges (?:usd|inr)|page \d|gstin|settlement|remit|"
                 r"payment terms|terms \d+|quote the|bank|swift|ifsc|bill to|customer)\b", s, re.I) and \
            not re.search(r"late fee on", s, re.I):
        return None
    lm = match_label(s, labels)
    late = re.search(r"Late fee on ([A-Z0-9][A-Z0-9/\-]{4,}):?\s*([A-Z]{3})?\s*([\d,]+\.\d+) overdue,\s*month (\d+)\s*@\s*"
                     r"([\d.]+)%", s, re.I)
    if not lm and not late:
        return None
    work = s
    out = {"code": "LATE" if late else lm[0], "label": None, "awb": None, "master_awb": None, "lane": None,
           "pickup_date": None, "qty": None, "unit": None, "rate": None, "pct": None, "base": None, "amount": None,
           "ref": None, "late_fee": None}
    if late:
        out["late_fee"] = {"invoice_id": late[1], "principal": str(_num(late[3])), "month": int(late[4]),
                           "pct": late[5], "currency": late[2]}
        work = work[:late.start()] + " " + work[late.end():]
    mawb = re.search(r"\(MAWB ([A-Z0-9]+)\)", work)
    if mawb:
        out["master_awb"] = mawb[1]
        work = work.replace(mawb[0], " ")
    awbs = AWB_RE.findall(work)
    if awbs:
        out["awb"] = awbs[0]
        for a in set(awbs):
            work = work.replace(a, " ")
    ref = re.search(r"\(ref ([A-Z0-9\-]+)\)", work)
    if ref:
        work = work.replace(ref[0], " ")
    lane = LANE_RE.search(work)
    if lane:
        out["lane"] = lane[1]
        work = work.replace(lane[1], " ")
    dts = re.findall(DATE_TOKEN, work)
    for t in dts:
        d = parse_date(t, dayfirst)
        if d and out["pickup_date"] is None:
            out["pickup_date"] = d.isoformat()
        work = work.replace(t, " ", 1)
    mref = re.search(r"\b(\d{4}-\d{2})\b", work)
    if mref:
        out["ref"] = mref[1]
        work = work.replace(mref[1], " ", 1)
    if lm:
        # drop the label words
        words = re.findall(r"\S+", work)
        out["label"] = lm[1]
    pctm = PCT_RE.search(work)
    if pctm:
        out["pct"] = pctm[1]
        work = work[:pctm.start()] + " " + work[pctm.end():]
    qm = QTY_RE.search(work)
    if qm and not late:
        out["qty"] = str(D(qm[1]))
        out["unit"] = UNIT_MAP.get(qm[2].lower(), qm[2].lower())
        work = work[:qm.start()] + " " + work[qm.end():]
    if late:
        qm = re.search(r"(\d+)\s*months?", work)
        if qm:
            work = work[:qm.start()] + " " + work[qm.end():]
    # remaining decimal numbers (drop row numbers, SAC codes and other bare integers)
    nums = [m for m in NUM_RE.findall(work) if "." in m]
    vals = [_num(n) for n in nums]
    if late:
        principal = D(out["late_fee"]["principal"])
        p = D(out["late_fee"]["pct"])
        out["pct"] = out["late_fee"]["pct"]
        out["base"] = str(principal)
        out["amount"] = str(vals[-1]) if vals else None
        for v in vals:
            if r2(p / 100 * principal) == v:
                out["amount"] = str(v)
        return out
    if out["pct"] is not None:
        p = D(out["pct"])
        pair = None
        for a in vals:
            for b in vals:
                if a is not b and r2(p / 100 * a) == b:
                    pair = (a, b)
        if pair is None and len(vals) >= 2:
            pair = (vals[0], vals[1]) if not order_amount_first else (vals[1], vals[0])
        if pair:
            out["base"], out["amount"] = str(pair[0]), str(pair[1])
        elif vals:
            out["amount"] = str(vals[-1])
        return out
    if out["qty"] is not None:
        q = D(out["qty"])
        pair = None
        for a in vals:
            for b in vals:
                if a is not b and r2(q * a) == b:
                    pair = (a, b)
        if pair is None and len(vals) >= 2:
            if order_amount_first:
                pair = (vals[1], vals[0])
            else:
                pair = (vals[0], vals[1])
        if pair:
            out["rate"], out["amount"] = str(pair[0]), str(pair[1])
        elif vals:
            out["amount"] = str(vals[-1])
        return out
    if vals:
        out["amount"] = str(vals[-1])
        if len(vals) >= 2:
            out["rate"] = str(vals[-2])
    return out


def read_invoice(doc: dict, labels) -> dict | None:
    pages = lines_of(doc)
    flat = [(p, i, s) for p, ls in enumerate(pages, 1) for i, s in enumerate(ls, 1)]
    inv_id = src_id = None
    kind = "invoice"
    for p, i, s in flat:
        for pat in INVOICE_ID_PATTERNS:
            m = re.search(pat, s, re.I if "STATEMENT" not in pat else 0)
            if m and not re.match(r"(DATE|NO|NUMBER)$", m[1], re.I):
                inv_id, src_id = m[1].rstrip(".,"), [p, i]
                if "STATEMENT" in pat:
                    kind = "statement"
                break
        if inv_id:
            break
    if not inv_id:
        # OCR text: no spaces ("ChargesadviceCDI-3031-0306")
        for p, i, s in flat:
            m = re.search(r"(?:INVOICE|Freightbillno\.?|Chargesadvice|STATEMENTOFACCOUNT)\s*([A-Z0-9][A-Z0-9/\-]{4,})",
                          s.replace(" ", ""), re.I)
            if m:
                inv_id, src_id = m[1], [p, i]
                break
    if not inv_id:
        return None
    dayfirst = _dayfirst(pages)
    text = "\n".join(s for _, _, s in flat)
    ntext = text.replace(" ", "")

    def date_after(*keys):
        for k in keys:
            m = re.search(k + r"\s*:?\s*" + DATE_TOKEN, text, re.I)
            if m:
                return parse_date(m[1], dayfirst).isoformat()
            m = re.search(k.replace(" ", "") + r"\s*:?\s*" + DATE_TOKEN.replace(" ", ""), ntext, re.I)
            if m:
                d = parse_date(m[1], dayfirst)
                if d:
                    return d.isoformat()
        return None

    inv_date = date_after("Invoice date", "Issued on", "Document date", "Statement date", "Date of issue", "Dated")
    due = date_after("Due date", "Settle by", "Payable by", "Payment due", "Due by", "Due")
    cm = re.search(r"(?:Contract(?: reference)?|Service agreement|Agreement)\s*:?\s*([A-Z]{2,4}-[A-Z]{2}-[A-Z]{2,4}-[A-Z0-9]+)",
                   text) or re.search(r"([A-Z]{2,4}-[A-Z]{2}-[A-Z]{2,4}-[A-Z0-9]+)", text)
    cur = re.search(r"\b(USD|INR|EUR|GBP|SGD)\b", text)
    # column order: does the amount column come before the rate column?
    order_amount_first = None
    for _, _, s in flat:
        ls_ = s.lower()
        if ("rate" in ls_ or "unit price" in ls_) and ("amount" in ls_ or "net" in ls_):
            ia = min([ls_.find(w) for w in ("amount", "net") if w in ls_])
            ir = min([ls_.find(w) for w in ("rate", "unit price") if w in ls_])
            order_amount_first = ia < ir
            break
    lines = []
    for p, i, s in flat:
        if (p, i) == tuple(src_id):
            continue
        ln = parse_invoice_line(s, labels, dayfirst, order_amount_first)
        if ln and ln.get("amount") is not None:
            ln["src"] = [p, i]
            ln["text"] = s
            lines.append(ln)
    total = None
    for pat in (r"Total payable\s*([A-Z]{3})?\s*([\d,]+\.\d+)", r"Balance due\s*([A-Z]{3})?\s*([\d,]+\.\d+)",
                r"Total\s*([A-Z]{3})\s*\|?\s*([\d,]+\.\d+)", r"Total\s*([A-Z]{3})?\s*\|?\s*([\d,]+\.\d+)"):
        m = re.search(pat, text) or re.search(pat.replace(r"\s*", "").replace(" ", ""), ntext)
        if m:
            total = str(_num(m[2]))
            break
    return {"type": "invoice", "invoice_id": inv_id, "doc_kind": kind, "invoice_date": inv_date, "due_date": due,
            "contract_id": cm[1] if cm else None, "currency": cur[1] if cur else "USD", "total": total,
            "lines": lines, "src": src_id}


def read_credit_note(doc: dict) -> dict | None:
    pages = lines_of(doc)
    text = "\n".join("\n".join(ls) for ls in pages)
    m, p, i = find(pages, r"CREDIT NOTE\s+([A-Z0-9][A-Z0-9/\-]+)", re.I)
    if not m:
        return None
    dt = re.search(r"(?:Credit|Issue|Document) date\s*:?\s*" + DATE_TOKEN, text)
    cid = re.search(r"([A-Z]{2,4}-[A-Z]{2}-[A-Z]{2,4}-[A-Z0-9]+)", text)
    ty = re.search(r"Credit type\s+(\w+)(?:\s*\|\s*period\s+(\S+))?", text)
    total = re.search(r"Total credit\s*([A-Z]{3})?\s*([\d,]+\.\d+)", text)
    ctype = (ty[1].lower() if ty else "other")
    ctype = {"improvement": "promised_credit", "rebate": "rebate", "margin": "margin_step_down"}.get(ctype, ctype)
    period = ty[2] if ty and ty[2] else None
    if not period:
        pm = re.search(r"\b(\d{4}-Q[1-4]|CY\d+)\b", text)
        period = pm[1] if pm else None
    refs = re.findall(r"(?:invoice|against|ref(?:erence)?)\s+([A-Z]{2,4}[A-Z0-9/\-]{4,})", text, re.I)
    tl = None
    if total:
        _, tp, ti = find(pages, r"Total credit")
        tl = [tp, ti]
    return {"type": "credit_note", "credit_note_id": m[1], "date": parse_date(dt[1]).isoformat() if dt else None,
            "contract_id": cid[1] if cid else None, "credit_type": ctype, "period": period,
            "currency": (total[1] if total and total[1] else "USD"), "amount": str(_num(total[2])) if total else None,
            "references": [r for r in refs if r != m[1]], "src": [p, i], "amount_src": tl}


def read_awb(doc: dict) -> dict | None:
    pages = lines_of(doc)
    text = "\n".join("\n".join(ls) for ls in pages)
    m, p, i = find(pages, r"(MASTER )?AIR WAYBILL\s+(\S+)", re.I)
    if not m:
        return None
    f = {"type": "awb", "awb": m[2], "is_master": bool(m[1]), "src": [p, i]}
    mm = re.search(r"Master AWB\s+(\S+)", text)
    f["master_awb"] = mm[1] if mm else None
    ex = re.search(r"Executed on\s+" + DATE_TOKEN + r"(?:\s+at\s+(\S+))?", text)
    f["date"] = parse_date(ex[1]).isoformat() if ex else None
    _, dp, di = find(pages, r"Executed on")
    f["date_src"] = [dp, di] if dp else None
    od = re.search(r"Origin\s+(\S+)\s*\|\s*Destination\s+(\S+)", text)
    f["origin"], f["destination"] = (od[1], od[2]) if od else (None, None)
    pc = re.search(r"^Pieces\s+(\d+)", text, re.M)
    f["pieces"] = int(pc[1]) if pc else None
    aw = re.search(r"Actual weight\s+([\d.]+)", text)
    f["actual_weight"] = aw[1] if aw else None
    cw, cp, ci = find(pages, r"^Chargeable weight\s+([\d.]+)")
    f["chargeable_weight"] = cw[1] if cw else None
    f["chargeable_src"] = [cp, ci] if cw else None
    dims = re.findall(r"(\d+)x(\d+)x(\d+)\s*cm\s*x\s*(\d+)", text)
    f["dims"] = [{"l": a, "w": b, "h": c, "count": n} for a, b, c, n in dims]
    tw, tp_, ti_ = find(pages, r"Total pieces\s+(\d+)\s*\|\s*Total chargeable weight\s+([\d.]+)")
    f["total_chargeable_weight"] = tw[2] if tw else None
    f["total_src"] = [tp_, ti_] if tw else None
    f["houses"] = [{"awb": a, "pieces": int(b), "weight": c} for a, b, c in
                   re.findall(r"House\s+(\S+?):\s*(\d+) pieces?,\s*([\d.]+)\s*kg", text)]
    g = re.search(r"Nature of goods:\s*(.+)", text)
    f["goods"] = g[1] if g else None
    di = re.search(r"UN1845 dry ice\s*([\d.]+)", text, re.I)
    f["dry_ice_kg"] = di[1] if di else None
    f["un_numbers"] = sorted(set(re.findall(r"\bUN(\d{4})\b", text)))
    return f


def read_dgd(doc: dict) -> dict | None:
    pages = lines_of(doc)
    m, p, i = find(pages, r"DECLARATION FOR (?:AWB )?(\S+)", re.I)
    if not m:
        return None
    items = []
    for pp, ls in enumerate(pages, 1):
        for ii, s in enumerate(ls, 1):
            u = re.match(r"UN(\d{4})\s+(.+)", s)
            if u:
                items.append({"un": u[1], "description": u[2], "refrigerant_only": bool(
                    re.search(r"refrigerant only|used solely as (?:a )?refrigerant", u[2], re.I)),
                    "refrigerant": "refrigerant" in u[2].lower(), "src": [pp, ii]})
    return {"type": "dg_declaration", "awb": m[1], "items": items, "src": [p, i]}


def read_table(doc: dict) -> dict | None:
    """Tracking exports and AP registers (any sheet whose header names the columns)."""
    pages = lines_of(doc)
    for p, ls in enumerate(pages, 1):
        if not ls or not ls[0]:
            continue
        hdr = [h.strip().lower() for h in ls[0].split(" | ")]
        if any("event" in h for h in hdr) and any("awb" in h or "waybill" in h or "shipment" in h for h in hdr):
            ix = {k: next((n for n, h in enumerate(hdr) if any(w in h for w in ws)), None) for k, ws in {
                "awb": ("awb", "waybill", "shipment"), "event": ("event", "status"), "date": ("date",),
                "station": ("station", "location"), "reason": ("reason",)}.items()}
            evs = []
            for i, s in enumerate(ls[1:], 2):
                c = s.split(" | ")
                if len(c) <= max(v for v in ix.values() if v is not None):
                    continue
                d = parse_date(c[ix["date"]])
                if not d:
                    continue
                ev = re.sub(r"[^A-Z_]", "_", c[ix["event"]].strip().upper())
                evs.append({"awb": c[ix["awb"]].strip(), "event": ev, "date": d.isoformat(),
                            "station": c[ix["station"]].strip() if ix["station"] is not None else None,
                            "reason": (c[ix["reason"]].strip() if ix["reason"] is not None and
                                       c[ix["reason"]].strip() not in ("-", "") else None),
                            "src": [p, i]})
            return {"type": "tracking", "events": evs, "src": [p, 1]}
        if any("invoice" in h for h in hdr) and any("status" in h for h in hdr):
            ix = {k: next((n for n, h in enumerate(hdr) if any(w in h for w in ws)), None) for k, ws in {
                "id": ("invoice no", "invoice", "document"), "date": ("invoice date",), "due": ("due",),
                "amount": ("amount",), "status": ("status",), "paid": ("paid",)}.items()}
            rows = []
            for i, s in enumerate(ls[1:], 2):
                c = s.split(" | ")
                if len(c) < 3:
                    continue
                amt = c[ix["amount"]] if ix["amount"] is not None and ix["amount"] < len(c) else ""
                cur = re.search(r"[A-Z]{3}", amt)
                rows.append({"invoice_id": c[ix["id"]].strip(),
                             "invoice_date": (parse_date(c[ix["date"]]) or None) and parse_date(c[ix["date"]]).isoformat(),
                             "due_date": (parse_date(c[ix["due"]]) or None) and parse_date(c[ix["due"]]).isoformat(),
                             "currency": cur[0] if cur else None, "amount": str(D(amt)) if D(amt) is not None else None,
                             "status": c[ix["status"]].strip().lower() if ix["status"] is not None else None,
                             "paid_date": (parse_date(c[ix["paid"]]).isoformat()
                                           if ix["paid"] is not None and ix["paid"] < len(c) and parse_date(c[ix["paid"]])
                                           else None),
                             "src": [p, i]})
            m = re.search(r"\d{4}-\d{2}-\d{2}", doc["name"] + " " + doc["meta"].get("subject", ""))
            return {"type": "ledger", "as_of": m[0] if m else doc["received"], "rows": rows, "src": [p, 1]}
    return None


def read_annex(doc: dict) -> dict | None:
    pages = lines_of(doc)
    m, p, i = find(pages, r"(Annex [A-Z](?: rev(?:ision)? ?\d+| update)?)")
    if not m:
        return None
    text = "\n".join("\n".join(ls) for ls in pages)
    cid = re.search(r"([A-Z]{2,4}-[A-Z]{2}-[A-Z]{2,4}-[A-Z0-9]+)", text)
    eff = re.search(r"(?:applies from|effective(?: from)?)\s+" + DATE_TOKEN, text, re.I)
    rates, fx = [], []
    for pp, ls in enumerate(pages, 1):
        for ii, s in enumerate(ls, 1):
            row = parse_rate_row(s, [])
            if row:
                row["src"] = [pp, ii]
                rates.append(row)
            fm = re.match(r"(\d{4}-\d{2})\s+(\d+(?:\.\d+)?)\s*$", s)
            if fm:
                fx.append({"month": fm[1], "rate": fm[2], "src": [pp, ii]})
    return {"type": "annex", "name": m[1], "contract_id": cid[1] if cid else None,
            "effective_from": parse_date(eff[1]).isoformat() if eff else None, "rates": rates, "fx": fx,
            "lanes": sorted({r["lane"] for r in rates}), "src": [p, i]}


# ---- entry point ---------------------------------------------------------------------------------------------------
def classify(doc: dict) -> str:
    if doc["ext"] == "eml":
        return "email"
    head = norm_label(" ".join(doc["pages"][0].split("\n")[:8]) if doc["pages"] else "")
    name = doc["name"].lower()
    if doc["ext"] in ("xlsx", "xlsm"):
        if "statementofaccount" in head or "invoice" in head and "status" not in head:
            return "invoice"
        return "table"
    if "amendmentno" in head:
        return "amendment"
    if "masterservicesagreement" in head and "agreementno" in head:
        return "contract"
    if "creditnote" in head:
        return "credit_note"
    if "airwaybill" in head:
        return "awb"
    if "declaration" in head:
        return "dgd"
    if re.search(r"annex[a-z](rev\d+|update)", head):
        return "annex"
    if any(k in head for k in ("invoice", "freightbill", "chargesadvice", "statementofaccount")) or \
            name.startswith(("inv", "cd", "tx")):
        return "invoice"
    return "other"


def read(doc: dict, labels: dict[str, str]) -> list[dict]:
    kind = classify(doc)
    try:
        if kind == "email":
            return read_email(doc, labels)
        f = {"contract": read_contract, "amendment": read_amendment, "credit_note": read_credit_note,
             "awb": read_awb, "dgd": read_dgd, "table": read_table, "annex": read_annex}.get(kind)
        if f:
            r = f(doc)
            return [r] if r else []
        if kind in ("invoice", "other"):
            r = read_invoice(doc, labels)
            return [r] if r else []
    except Exception as e:                                  # noqa: BLE001 - a reader bug must not stop ingest
        return [{"type": "error", "error": f"{type(e).__name__}: {e}"[:300]}]
    return []
