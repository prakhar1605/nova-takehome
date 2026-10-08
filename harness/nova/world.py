"""What the harness knows on a given date: every fact from documents received by end of `as_of` (UTC), indexed for
the checks, plus the helpers every check shares (who has authority, when a shipment was picked up, how to quote a
source line as evidence).

Authority levels (GLOSSARY "Which document wins"):
    1  signed MSA / signed amendment / its annexes and schedules
    2  an email from the Schedule 1 person for that category; a fact a person confirmed in feedback
    3  records: tracking export, milestone emails, AWBs, declarations, customs notices, the payment ledger, invoices
    4  anyone else's email, unsigned drafts, proposals
    5  quoted text without its original, text aimed at automated readers, anything from outside the two companies
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from .facts import DEFAULT_ALIASES
from .util import D, norm_invoice, norm_label, norm_person, parse_date


class Doc:
    __slots__ = ("id", "email", "received", "received_at", "meta", "pages", "scan", "reader")

    def __init__(self, d: dict, reader: str):
        self.id, self.email, self.received = d["id"], d["email"], parse_date(d["received"])
        self.received_at, self.meta, self.pages, self.scan = d["received_at"], d["meta"], d["pages"], d.get("scan", False)
        self.reader = reader

    def lines(self, page: int) -> list[str]:
        return self.pages[page - 1].split("\n") if 1 <= page <= len(self.pages) else []


class World:
    def __init__(self, store, as_of: str):
        self.as_of = parse_date(as_of)
        self.store = store
        self.docs: dict[str, Doc] = {}
        self.facts: list[tuple[Doc, dict]] = []
        for d in store.docs():
            if parse_date(d["received"]) > self.as_of:
                continue
            fx = store.facts(d["id"]) or {}
            doc = Doc(d, fx.get("reader", "rules"))
            self.docs[doc.id] = doc
            for f in fx.get("facts") or []:
                self.facts.append((doc, f))
        self._index()

    # ---- indexing ---------------------------------------------------------------------------------------------------
    def _index(self):
        self.contracts: dict[str, tuple[Doc, dict]] = {}
        self.amendments, self.annexes, self.notices, self.waivers = [], [], [], []
        self.extensions = defaultdict(list)
        self.events = defaultdict(list)            # awb -> [(event, date, doc, fact-or-row, source)]
        self.customs = {}
        self.invoices = defaultdict(list)          # invoice id -> [(doc, fact)] in arrival order
        self.credit_notes, self.ledgers = [], []
        self.awbs, self.dgds = {}, {}
        self.covers = {}                           # email id -> cover fact
        self.injections = []
        for doc, f in self.facts:
            t = f.get("type")
            if t == "contract" and f.get("contract_id"):
                self.contracts[f["contract_id"]] = (doc, f)
            elif t == "amendment":
                self.amendments.append((doc, f))
            elif t == "annex":
                self.annexes.append((doc, f))
            elif t == "rate_notice":
                self.notices.append((doc, f))
            elif t == "waiver":
                self.waivers.append((doc, f))
            elif t == "due_date_extension" and f.get("invoice_id"):
                self.extensions[norm_invoice(f["invoice_id"])].append((doc, f))
            elif t == "shipment_event" and f.get("awb"):
                self.events[f["awb"]].append((f["event"], parse_date(f["date"]), doc, f, "email"))
            elif t == "customs_hold" and f.get("awb"):
                self.customs[f["awb"]] = (doc, f)
            elif t == "tracking":
                for e in f.get("events") or []:
                    if e.get("awb") and parse_date(e.get("date")):
                        self.events[e["awb"]].append((e["event"], parse_date(e["date"]), doc, e, "tracking"))
            elif t == "invoice" and f.get("invoice_id"):
                self.invoices[norm_invoice(f["invoice_id"])].append((doc, f))
            elif t == "credit_note":
                self.credit_notes.append((doc, f))
            elif t == "ledger":
                self.ledgers.append((doc, f))
            elif t == "awb" and f.get("awb"):
                self.awbs[f["awb"]] = (doc, f)
            elif t == "dg_declaration" and f.get("awb"):
                self.dgds[f["awb"]] = (doc, f)
            elif t == "invoice_cover":
                self.covers[doc.email] = (doc, f)
            elif t == "injection":
                self.injections.append((doc, f))
        self.ledgers.sort(key=lambda x: (str(x[1].get("as_of") or x[0].received), x[0].received_at))
        for k in self.invoices:
            self.invoices[k].sort(key=lambda x: x[0].received_at)

    # ---- evidence ---------------------------------------------------------------------------------------------------
    def ev(self, doc: Doc, src) -> dict | None:
        """A citation: the whole source line(s), so the grader finds the quote on that page."""
        if not src or not doc:
            return None
        p, a = int(src[0]), int(src[1])
        b = int(src[2]) if len(src) > 2 else a
        ls = doc.lines(p)
        if not ls or a < 1 or a > len(ls):
            return None
        b = min(max(b, a), len(ls))
        q = " ".join(x.strip() for x in ls[a - 1:b])
        while len(re.sub(r"\s", "", q)) < 12 and b < len(ls):         # too short to be a citation: add a line
            b += 1
            q = " ".join(x.strip() for x in ls[a - 1:b])
        if len(re.sub(r"\s", "", q)) < 12:
            return None
        return {"doc": doc.id, "page": p, "quote": q}

    # ---- contracts and authority ------------------------------------------------------------------------------------
    def contract(self, cid: str | None):
        if cid and cid in self.contracts:
            return self.contracts[cid]
        return None

    def contract_for_invoice(self, inv: dict):
        c = self.contract(inv.get("contract_id"))
        if c:
            return c
        # no reference: the contract in the invoice's currency whose Annex A has the invoice's lanes
        lanes = {ln.get("lane") for ln in inv.get("lines") or [] if ln.get("lane")}
        best = None
        for cid, (doc, cf) in self.contracts.items():
            have = {r["lane"] for r in cf.get("rates") or []}
            score = len(lanes & have) + (1 if (cf.get("currency") or "USD") == (inv.get("currency") or "USD") else 0)
            if best is None or score > best[0]:
                best = (score, (doc, cf))
        return best[1] if best else None

    def schedule1(self, cf: dict) -> list[dict]:
        return cf.get("schedule1") or []

    def person(self, cf: dict, *, email: str | None = None, name: str | None = None) -> dict | None:
        for p in self.schedule1(cf):
            if email and p.get("email") and p["email"].lower() == email.lower():
                return p
        for p in self.schedule1(cf):
            if name and norm_person(p.get("name")) == norm_person(name):
                return p
        return None

    def authorised(self, cf: dict, doc: Doc, category: str, party: str | None = None, on: date | None = None,
                   feedback=None) -> tuple[bool, dict | None, str]:
        """Does this email's sender hold `category` for this contract (and `party`, default the carrier)?
        -> (yes, Schedule 1 row or None, how: 'schedule1' | 'feedback:<item id>' | 'none').
        The sender must match Schedule 1 by address (the notice address); a name match alone is not enough."""
        party = party or cf.get("carrier_party")
        sender = (doc.meta or {}).get("from_email", "")
        p = self.person(cf, email=sender)
        if p and category in (p.get("categories") or []) and (party is None or p.get("party") == party):
            return True, p, "schedule1"
        if feedback is not None:
            fid = feedback.authority(cf.get("contract_id"), (doc.meta or {}).get("from_name"), sender, category,
                                     on or doc.received)
            if fid:
                return True, p, f"feedback:{fid}"
        return False, p, "none"

    def holder(self, cf: dict, category: str) -> dict:
        """Who holds a missing document of this kind: the carrier's Schedule 1 contact for that category."""
        party = cf.get("carrier_party")
        for p in self.schedule1(cf):
            if p.get("party") == party and category in (p.get("categories") or []):
                return {"party": party, "person": p["name"], "role": p.get("title")}
        return self.kam(cf)

    def kam(self, cf: dict) -> dict:
        party = cf.get("carrier_party")
        for p in self.schedule1(cf):
            if p.get("party") == party and ("notices" in (p.get("categories") or []) or
                                            "key account" in (p.get("title") or "").lower()):
                return {"party": party, "person": p["name"], "role": p.get("title")}
        return {"party": party}

    def billing_contact(self, cf: dict) -> dict | None:
        party = cf.get("carrier_party")
        for p in self.schedule1(cf):
            if p.get("party") == party and "billing_disputes" in (p.get("categories") or []):
                return {"name": p["name"], "email": p.get("email"), "role": "billing_disputes"}
        return None

    def is_carrier_sender(self, cf: dict, doc: Doc) -> bool:
        sender = (doc.meta or {}).get("from_email", "")
        dom = sender.split("@")[-1]
        party = cf.get("carrier_party")
        doms = {p["email"].split("@")[-1] for p in self.schedule1(cf) if p.get("party") == party and p.get("email")}
        return bool(dom) and dom in doms

    # ---- charge codes -----------------------------------------------------------------------------------------------
    def alias_index(self, cf: dict | None) -> dict[str, str]:
        idx = {}
        for code, labels in DEFAULT_ALIASES.items():
            for lab in labels:
                idx[norm_label(lab)] = code
        if cf:
            for a in cf.get("accessorials") or []:
                idx[norm_label(a.get("name"))] = a["code"]
            for a in cf.get("aliases") or []:
                for lab in a.get("labels") or []:
                    idx[norm_label(lab)] = a["code"]
        return idx

    def code_of(self, line: dict, cf: dict | None) -> str | None:
        """Map a billed label to its Annex E code. The contract's own list wins over the reader's guess."""
        if line.get("late_fee"):
            return "LATE"
        idx = self.alias_index(cf)
        desc = line.get("description") or line.get("label") or ""
        nd = norm_label(re.sub(r"\(MAWB [^)]*\)|\(ref [^)]*\)", "", str(desc)))
        if nd in idx:
            return idx[nd]
        best = None
        for lab, code in idx.items():
            if len(lab) >= 3 and lab in nd and (best is None or len(lab) > len(best[0])):
                best = (lab, code)
        if best:
            return best[1]
        return line.get("code")

    # ---- shipments --------------------------------------------------------------------------------------------------
    def pickup(self, awb: str | None, line: dict | None = None, inv_doc: Doc | None = None, cf: dict | None = None):
        """The pickup date, from the first source held: tracking PICKED_UP, a carrier 'picked up' email, the AWB
        date, the date printed on the invoice. -> (date, source, evidence) or (None, None, None)."""
        if awb:
            evs = self.events.get(awb, [])
            for e, d, doc, row, src in evs:
                if e == "PICKED_UP" and src == "tracking":
                    return d, "tracking", self.ev(doc, row.get("src"))
            for e, d, doc, row, src in evs:
                if e == "PICKED_UP" and src == "email" and (cf is None or self.is_carrier_sender(cf, doc)):
                    return d, "carrier_email", self.ev(doc, row.get("src"))
            if awb in self.awbs:
                doc, f = self.awbs[awb]
                if parse_date(f.get("date")):
                    return parse_date(f["date"]), "awb", self.ev(doc, f.get("date_src") or f.get("src"))
        if line and parse_date(line.get("pickup_date")):
            return parse_date(line["pickup_date"]), "invoice", self.ev(inv_doc, line.get("src")) if inv_doc else None
        return None, None, None

    def milestone(self, awb: str, events: tuple[str, ...]):
        """First date of any of these events (tracking first, then emails) -> (date, evidence)."""
        best = None
        for e, d, doc, row, src in self.events.get(awb, []):
            if e in events:
                key = (0 if src == "tracking" else 1, d)
                if best is None or key < best[0]:
                    best = (key, d, self.ev(doc, row.get("src")))
        return (best[1], best[2]) if best else (None, None)

    # ---- the payment ledger ----
    @staticmethod
    def status(row) -> str:
        from .rules_reader import canon_status
        return canon_status(str((row or {}).get("status") or ""))

    def ledger_row(self, invoice_id: str):
        """The newest ledger row for an invoice -> (row, doc, ledger as_of) or (None, None, None)."""
        want = norm_invoice(invoice_id)
        for doc, f in reversed(self.ledgers):
            for r in f.get("rows") or []:
                if norm_invoice(r.get("invoice_id")) == want:
                    return r, doc, parse_date(f.get("as_of")) or doc.received
        return None, None, None

    def latest_ledger_date(self):
        if not self.ledgers:
            return None
        doc, f = self.ledgers[-1]
        return parse_date(f.get("as_of")) or doc.received

    # ---- money ------------------------------------------------------------------------------------------------------
    def fx(self, cf: dict, month: str):
        """INR per 1 USD for a calendar month, from Annex F (or a held Annex F update) -> (rate, evidence)."""
        for doc, a in self.annexes:
            if a.get("contract_id") in (None, cf.get("contract_id")):
                for r in a.get("fx") or []:
                    if r.get("month") == month:
                        return D(r["rate"]), self.ev(doc, r.get("src"))
        cdoc = self.contracts.get(cf.get("contract_id"), (None,))[0]
        for r in cf.get("fx") or []:
            if r.get("month") == month:
                return D(r["rate"]), self.ev(cdoc, r.get("src"))
        return None, None
