"""The six checks (plus conflict and cannot_determine), each turning the world into candidate items.

An internal item is a dict:
    key         ("overcharge", "TX-1") | ("cashback", cid, period, type) | ("expiry", cid) | ("late_fee_risk", inv)
    verdict     finding | conflict | cannot_determine
    amount      Decimal USD (finding), range [lo, hi] (conflict), deadline (expiry / risk)
    remedy      dispute | claim_credit | request_document | alert | payment_reminder | track_credit | escalate
    evidence    [{doc, page, quote}]
    gate        what the unattended-action test needs: pickup_from_record, missing, conflict lanes, authority
    learned     feedback item that changed it, if any
"""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

from .pricing import price_invoice
from .util import (D, ZERO, add_years, days, norm_invoice, norm_label, parse_date, pct_of, quarter_bounds,
                   quarter_of, r2, ym)

SHIPMENT_CODES = {"FRT", "FUEL", "SEC", "DG", "DRYICE", "STOR", "DLV", "CUSTOMS", "PEAK", "DUTY", "HDL", "COST",
                  "MARGIN"}
MONTHLY_CODES = {"ACCT", "SPROG", "PPROG"}


def _evs(*groups):
    out, seen = [], set()
    for g in groups:
        for e in (g if isinstance(g, list) else [g]):
            if e and (e["doc"], e["page"], e["quote"]) not in seen:
                seen.add((e["doc"], e["page"], e["quote"]))
                out.append(e)
    return out


def line_id(doc, ln):
    return (doc.id, tuple(ln.get("src") or ()))


class Checks:
    def __init__(self, world, fb):
        self.w = world
        self.fb = fb
        self.items: dict[tuple, dict] = {}
        self.claimed: set = set()                # invoice lines already explained by a waiver or a duplicate
        self.priced: dict[str, dict] = {}
        self.unique = self._unique_invoices()

    # ---- helpers ----------------------------------------------------------------------------------------------------
    def _unique_invoices(self):
        """One copy per invoice id (the first received); resends are handled by the duplicate check."""
        out = {}
        for iid, copies in self.w.invoices.items():
            doc, f = copies[0]
            out[iid] = (doc, f)
        return out

    def usd(self, cf, inv, amount: Decimal):
        """Invoice-currency amount -> (USD amount, fx evidence, missing-doc dict or None). INR converts once, at the
        Annex F rate for the month of the invoice date."""
        cur = (inv.get("currency") or (cf or {}).get("currency") or "USD").upper()
        if cur == "USD":
            return r2(amount), None, None
        month = ym(inv.get("invoice_date"))
        rate, ev = self.w.fx(cf, month)
        if rate is None:
            return None, None, {"name": f"Annex F exchange rate for {month}", "holder": self.w.holder(cf, "treasury")}
        return r2(amount / rate), ev, None

    def add(self, item):
        k = item["key"]
        old = self.items.get(k)
        if old is None or (old["verdict"] != "finding" and item["verdict"] == "finding"):
            self.items[k] = item

    def price(self, iid):
        if iid not in self.priced:
            doc, inv = self.unique[iid]
            self.priced[iid] = price_invoice(self.w, doc, inv, self.fb, ("overcharge", iid))
        return self.priced[iid]

    # ---- duplicates -------------------------------------------------------------------------------------------------
    def duplicates(self):
        w = self.w
        # (a) the same invoice number sent again, asking for payment of a copy already paid
        for iid, copies in w.invoices.items():
            if len(copies) < 2:
                continue
            first_doc, inv = copies[0]
            row, ldoc, _ = w.ledger_row(iid)
            if not row:
                continue
            status = w.status(row)
            paid = parse_date(row.get("paid_date"))
            for doc, f in copies[1:]:
                if doc.email == first_doc.email:
                    continue
                cv = w.covers.get(doc.email)
                asks = True if cv is None else bool(cv[1].get("asks_payment")) and not cv[1].get("copy")
                if not asks:
                    continue
                ok = (status == "paid" and paid is not None and paid <= doc.received) or status == "scheduled"
                if not ok:
                    continue
                c = w.contract_for_invoice(inv)
                cf = c[1] if c else {}
                total = D(inv.get("total")) or sum((D(l.get("amount")) or ZERO for l in inv.get("lines") or []), ZERO)
                amt, fxev, miss = self.usd(cf, inv, total)
                ev = _evs(w.ev(ldoc, row.get("src")), w.ev(cv[0], cv[1].get("src")) if cv else w.ev(doc, [1, 1]),
                          w.ev(doc, f.get("src")), fxev)
                for ln in inv.get("lines") or []:
                    self.claimed.add(line_id(first_doc, ln))
                self.add(self._mk(("duplicate", iid), "dispute", ev, c, amount=amt, missing=miss, conf=0.95,
                                  amount_cur=total, variant="resend"))
                break
        # (b) two invoice numbers billing the same shipment charge
        seen = defaultdict(list)
        for iid, (doc, inv) in self.unique.items():
            c = w.contract_for_invoice(inv)
            cf = c[1] if c else None
            for ln in inv.get("lines") or []:
                code = w.code_of(ln, cf)
                amt = D(ln.get("amount"))
                if not amt or amt <= 0:
                    continue
                if code in SHIPMENT_CODES and ln.get("awb"):
                    k = (ln["awb"], code)
                elif code in MONTHLY_CODES and ln.get("ref"):
                    k = ((cf or {}).get("contract_id"), code, ln["ref"])
                else:
                    continue
                seen[k].append((parse_date(inv.get("invoice_date")) or doc.received, doc.received_at, iid, doc, inv, ln))
        dup_lines = defaultdict(list)
        for k, hits in seen.items():
            if len({h[2] for h in hits}) < 2:
                continue
            hits.sort(key=lambda h: (h[0], h[1]))
            first = hits[0]
            for h in hits[1:]:
                if h[2] != first[2]:
                    dup_lines[h[2]].append((h, first))
        for iid, pairs in dup_lines.items():
            if ("duplicate", iid) in self.items:
                continue
            doc, inv = self.unique[iid]
            c = w.contract_for_invoice(inv)
            cf = c[1] if c else {}
            total = sum((D(h[5]["amount"]) for h, _ in pairs), ZERO)
            # credit notes against either invoice reduce the overlap
            credit = ZERO
            cevs = []
            ids = {iid} | {f[2] for _, f in pairs}
            for cdoc, cn in w.credit_notes:
                if cn.get("credit_type") in ("rebate", "promised_credit", "margin_step_down"):
                    continue
                if {norm_invoice(r) for r in cn.get("references") or []} & ids:
                    credit += D(cn.get("amount")) or ZERO
                    cevs.append(w.ev(cdoc, cn.get("src")))
            net = total - credit
            if net <= 0:
                continue
            amt, fxev, miss = self.usd(cf, inv, net)
            ev = []
            for h, f in pairs:
                ev += [w.ev(f[3], f[5].get("src")), w.ev(h[3], h[5].get("src"))]
                self.claimed.add(line_id(h[3], h[5]))
            if cf:
                for a in cf.get("aliases") or []:
                    if a["code"] in {w.code_of(h[5], cf) for h, _ in pairs} and \
                            len({norm_label(h[5].get("description") or h[5].get("label")) for h, _ in pairs} |
                                {norm_label(f[5].get("description") or f[5].get("label")) for _, f in pairs}) > 1:
                        ev.append(w.ev(c[0], a.get("src")))
            self.add(self._mk(("duplicate", iid), "dispute", _evs(ev, cevs, fxev), c, amount=amt, missing=miss,
                              conf=0.85, amount_cur=net, variant="rebilled"))

    # ---- waivers ----------------------------------------------------------------------------------------------------
    def _condition_met(self, wv: dict):
        cond = wv.get("condition")
        if not cond:
            return True
        if wv.get("condition_met") is not None:
            return bool(wv["condition_met"])
        m = re.search(r"([A-Z0-9][A-Z0-9/\-]{4,})\b.*?(?:paid|settled).*?(\d{4}-\d{2}-\d{2})", str(cond)) or \
            re.search(r"(?:paid|settled).*?([A-Z0-9][A-Z0-9/\-]{4,}).*?(\d{4}-\d{2}-\d{2})", str(cond))
        if m:
            row, _, _ = self.w.ledger_row(m[1])
            if row and parse_date(row.get("paid_date")) and parse_date(row["paid_date"]) <= parse_date(m[2]):
                return True
            return False
        return None

    def waivers(self):
        w = self.w
        by_inv = defaultdict(list)
        for wdoc, wv in w.waivers:
            if any(i[0].id == wdoc.id and i[1].get("src") == wv.get("src") for i in w.injections):
                continue
            awbs = set(wv.get("awbs") or [])
            months = set(wv.get("months") or [])
            invs = {norm_invoice(x) for x in wv.get("invoice_ids") or []}
            if not (awbs or months or invs):
                continue                                # names no shipment or month: not a waiver of anything
            for iid, (doc, inv) in self.unique.items():
                c = w.contract_for_invoice(inv)
                if not c:
                    continue
                cdoc, cf = c
                want = w.code_of({"description": wv.get("charge_label") or wv.get("charge_code"),
                                  "code": wv.get("charge_code")}, cf)
                for ln in inv.get("lines") or []:
                    if w.code_of(ln, cf) != want:
                        continue
                    hit = (ln.get("awb") in awbs) or (iid in invs) or \
                          (ln.get("ref") in months) or (ln.get("pickup_date") and ym(ln["pickup_date"]) in months)
                    if hit:
                        by_inv[iid].append((wdoc, wv, ln, c))
        for iid, hits in by_inv.items():
            doc, inv = self.unique[iid]
            key = ("waived_but_billed", iid)
            total, evs, learned, verdict, missing = ZERO, [], None, None, None
            for wdoc, wv, ln, (cdoc, cf) in hits:
                if wv.get("quoted"):
                    sender = wv.get("quoted_sender") or "the sender"
                    p = w.person(cf, name=sender)
                    holder = ({"party": p["party"], "person": p["name"], "role": p.get("title")}
                              if p and "waivers" in (p.get("categories") or []) else w.kam(cf))
                    missing = {"name": f"original email from {sender} waiving the "
                                       f"{wv.get('charge_label') or wv.get('charge_code')} "
                                       f"({wdoc.meta.get('subject', '')})", "holder": holder}
                    verdict = verdict or "cannot_determine"
                    evs += [w.ev(wdoc, wv.get("src")), w.ev(doc, ln.get("src"))]
                    continue
                ok, who, how = w.authorised(cf, wdoc, "waivers", feedback=self.fb)
                if how.startswith("feedback:"):
                    learned = how.split(":", 1)[1]
                if not ok:
                    if wv.get("cites_authority_doc"):
                        missing = {"name": wv["cites_authority_doc"], "holder": w.kam(cf)}
                        verdict = verdict or "cannot_determine"
                        evs += [w.ev(wdoc, wv.get("src")), w.ev(doc, ln.get("src"))]
                    continue
                met = self._condition_met(wv)
                if met is not True:
                    continue
                total += D(ln.get("amount")) or ZERO
                self.claimed.add(line_id(doc, ln))
                evs += [w.ev(wdoc, wv.get("src")), w.ev(cdoc, who.get("src")) if who else None,
                        w.ev(doc, ln.get("src")), w.ev(cdoc, (cf.get("clauses") or {}).get("waiver_authority"))]
                verdict = "finding"
            # one-off approval in feedback: the waiver covers the named months on this key only
            one = self.fb.waiver_months(key)
            if one and verdict != "finding":
                it, months = one
                for wdoc, wv, ln, (cdoc, cf) in hits:
                    mo = ln.get("ref") or (ym(ln["pickup_date"]) if ln.get("pickup_date") else None)
                    if mo in months:
                        total += D(ln.get("amount")) or ZERO
                        self.claimed.add(line_id(doc, ln))
                        evs += [w.ev(wdoc, wv.get("src")), w.ev(doc, ln.get("src"))]
                        verdict, learned = "finding", str(it.get("item_id"))
            if verdict is None:
                continue
            c = hits[0][3]
            if verdict == "finding" and total > 0:
                amt, fxev, miss = self.usd(c[1], inv, total)
                self.add(self._mk(key, "dispute", _evs(evs, fxev), c, amount=amt, missing=miss, conf=0.93,
                                  learned=learned, amount_cur=total))
            elif verdict == "cannot_determine":
                self.add(self._mk(key, "request_document", _evs(evs), c, verdict="cannot_determine",
                                  missing=missing, conf=0.8))

    # ---- overcharge, conflicts, missing rate documents --------------------------------------------------------------
    def overcharges(self):
        w = self.w
        for iid, (doc, inv) in self.unique.items():
            pr = self.price(iid)
            c = pr["contract"]
            if not c:
                continue
            cdoc, cf = c
            key = ("overcharge", iid)
            lines = [r for r in pr["lines"] if line_id(doc, r["line"]) not in self.claimed]
            missing = [r for r in lines if r["status"] == "missing"]
            conflict = [r for r in lines if r["status"] == "conflict"]
            over = [r for r in lines if r["status"] == "over"]
            fixed = sum((r["billed"] - r["correct"] for r in over), ZERO)
            learned = next((r["learned"] for r in lines if r.get("learned")), None)
            lanes = {r["lane"] for r in over + conflict + missing if r["lane"]}
            gate = {"pickup_record": any(r["pickup"][1] in ("awb", "invoice", None)
                                         for r in over + conflict if r["date_dependent"]),
                    "lanes": lanes}
            if missing:
                m = missing[0]["missing"]
                evs = _evs(m["evs"], [w.ev(doc, r["line"].get("src")) for r in missing])
                self.add(self._mk(key, "request_document", evs, c, verdict="cannot_determine",
                                  missing={"name": m["name"], "holder": w.holder(cf, "rates")}, conf=0.85, gate=gate))
                continue
            evs_over = []
            for r in over:
                evs_over += [w.ev(doc, r["line"].get("src"))] + r["evs"] + [r["pickup"][2] if r["date_dependent"] else None]
            if conflict:
                t1 = fixed + sum((r["billed"] - r["correct"] for r in conflict if r["correct"] is not None), ZERO)
                t2 = fixed + sum((r["billed"] - (r["alt"] if r["alt"] is not None else r["correct"])
                                  for r in conflict if r["correct"] is not None), ZERO)
                lo, hi = max(ZERO, min(t1, t2)), max(t1, t2)
                if hi <= 0:
                    continue
                a, fxev, miss = self.usd(cf, inv, lo)
                b, _, _ = self.usd(cf, inv, hi)
                evc = []
                for r in conflict:
                    evc += [w.ev(doc, r["line"].get("src"))] + r["evs"] + [r["pickup"][2]]
                if a == b:
                    self.add(self._mk(key, "dispute", _evs(evc, evs_over, fxev), c, amount=b, missing=miss, conf=0.8,
                                      gate=gate, amount_cur=hi))
                    continue
                srcs = []
                for r in conflict:
                    if r["code"] == "FRT":
                        srcs = r["source"].evs[:1] + r["alt_source"].evs[:1]
                self.add(self._mk(key, "escalate", _evs(srcs, evc, evs_over, fxev), c, verdict="conflict",
                                  rng=[a, b], conf=0.85, gate={**gate, "conflict": True}, conflict_lanes=lanes))
                continue
            if fixed > 0:
                amt, fxev, miss = self.usd(cf, inv, fixed)
                if miss:
                    self.add(self._mk(key, "request_document", _evs(evs_over), c, verdict="cannot_determine",
                                      missing=miss, conf=0.8))
                    continue
                conf = 0.95
                if gate["pickup_record"]:
                    conf -= 0.08
                if doc.scan:
                    conf -= 0.05
                if amt < Decimal("1.00"):
                    conf -= 0.15
                self.add(self._mk(key, "dispute", _evs(evs_over, fxev), c, amount=amt, conf=conf, gate=gate,
                                  learned=learned, amount_cur=fixed, deciding=[r.get("source") for r in over]))

    # ---- late fees --------------------------------------------------------------------------------------------------
    def due_date(self, iid: str, cf: dict | None):
        """The due date that counts: an extension from the carrier's payment-terms person, else the ledger's, else
        the invoice's. -> (date, evidence list, learned feedback id)."""
        w = self.w
        best, evs, learned = None, [], None
        for doc, ext in w.extensions.get(norm_invoice(iid), []):
            if ext.get("quoted"):
                continue
            c = w.contract(cf.get("contract_id")) if cf else None
            ok, who, how = w.authorised(cf, doc, "payment_terms", feedback=self.fb) if cf else (False, None, "none")
            if ok and parse_date(ext.get("new_due_date")):
                best = parse_date(ext["new_due_date"])
                evs = [w.ev(doc, ext.get("src")), w.ev(c[0], (cf.get("clauses") or {}).get("extension")) if c else None]
                learned = how.split(":", 1)[1] if how.startswith("feedback:") else None
        if best:
            return best, evs, learned
        row, ldoc, _ = w.ledger_row(iid)
        if row and parse_date(row.get("due_date")):
            return parse_date(row["due_date"]), [w.ev(ldoc, row.get("src"))], None
        if norm_invoice(iid) in self.unique:
            doc, inv = self.unique[norm_invoice(iid)]
            if parse_date(inv.get("due_date")):
                return parse_date(inv["due_date"]), [w.ev(doc, inv.get("src"))], None
        return None, [], None

    def fee_schedule(self, lf: dict, principal: Decimal, n: int) -> list[Decimal]:
        pct = D(lf.get("pct")) or ZERO
        fees, base = [], principal
        for _ in range(n):
            f = pct_of(pct, base)
            fees.append(f)
            if lf.get("compounding", True):
                base = base + f
        return fees

    def disputed_on(self, iid: str) -> Decimal:
        """Amounts we are disputing on an invoice, in its own currency (they never bear a late fee)."""
        tot = ZERO
        for k, it in self.items.items():
            if len(k) == 2 and k[1] == norm_invoice(iid) and it["verdict"] == "finding" and \
                    k[0] in ("overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed"):
                tot += it.get("amount_cur") or ZERO
        return tot

    def late_fees(self):
        w = self.w
        for iid, (doc, inv) in self.unique.items():
            lates = [ln for ln in inv.get("lines") or [] if ln.get("late_fee")]
            if not lates:
                continue
            total, evs, learned = ZERO, [], None
            c = None
            for ln in lates:
                lf_ = ln["late_fee"]
                xid = norm_invoice(lf_.get("invoice_id"))
                xrow, xldoc, _ = w.ledger_row(xid)
                xinv = self.unique.get(xid)
                xc = w.contract_for_invoice(xinv[1]) if xinv else w.contract_for_invoice(inv)
                if not xc:
                    continue
                c = c or xc
                xcdoc, xcf = xc
                lf = xcf.get("late_fee") or {}
                due, dev, lrn = self.due_date(xid, xcf)
                learned = learned or lrn
                paid = parse_date((xrow or {}).get("paid_date"))
                if due is None:
                    continue
                if paid is None:
                    if w.status(xrow) == "paid" or not xrow:
                        continue
                    paid = parse_date(inv.get("invoice_date")) or doc.received
                late_days = (paid - due).days
                grace = int(lf.get("grace_days") or 0)
                principal = D((xrow or {}).get("amount")) or (D(xinv[1].get("total")) if xinv else None) or \
                    D(lf_.get("principal")) or ZERO
                principal -= self.disputed_on(xid)
                n = 0 if late_days <= grace else -(-late_days // 30)
                fees = self.fee_schedule(lf, principal, n)
                m = int(lf_.get("month") or 1)
                correct = fees[m - 1] if 1 <= m <= len(fees) else ZERO
                billed = D(ln.get("amount")) or ZERO
                if billed - correct > 0:
                    total += billed - correct
                    evs += dev + [w.ev(xldoc, (xrow or {}).get("src")), w.ev(doc, ln.get("src")),
                                  w.ev(xcdoc, (xcf.get("clauses") or {}).get("late_fee"))]
            if total > 0 and c:
                amt, fxev, miss = self.usd(c[1], inv, total)
                self.add(self._mk(("late_fee_not_owed", iid), "dispute", _evs(evs, fxev), c, amount=amt, missing=miss,
                                  conf=0.93, learned=learned, amount_cur=total))

    def late_fee_risk(self):
        w = self.w
        latest = w.latest_ledger_date()
        for iid, (doc, inv) in self.unique.items():
            row, ldoc, lasof = w.ledger_row(iid)
            if row:
                if w.status(row) not in ("open", "disputed"):
                    continue
            elif latest is not None and latest >= doc.received:
                continue                     # the ledger postdates the invoice and doesn't list it: don't guess
            c = w.contract_for_invoice(inv)
            if not c:
                continue
            cdoc, cf = c
            due, dev, learned = self.due_date(iid, cf)
            if due is None or not (0 < (due - w.as_of).days <= 14):
                continue
            lf = cf.get("late_fee") or {}
            principal = (D(row.get("amount")) if row else None) or D(inv.get("total")) or ZERO
            principal -= self.disputed_on(iid)
            if principal <= 0 or not D(lf.get("pct")):
                continue
            fee = pct_of(D(lf["pct"]), principal)
            amt, fxev, miss = self.usd(cf, inv, fee)
            ev = _evs(w.ev(ldoc, row.get("src")) if row else w.ev(doc, inv.get("src")), dev,
                      w.ev(cdoc, (cf.get("clauses") or {}).get("late_fee")), fxev)
            it = self._mk(("late_fee_risk", iid), "payment_reminder", ev, c, amount=amt, missing=miss, conf=0.9,
                          learned=learned)
            it["deadline"] = due
            self.add(it)

    # ---- cashback and promised credits ------------------------------------------------------------------------------
    def qualifying(self, cid, cf, start, end):
        """Qualifying spend in USD for invoices dated start..end on this contract, net of credit notes dated in the
        period (rebate and improvement credits excepted). INR converts per invoice."""
        w = self.w
        qs = cf.get("qs") or {}
        acc = qs.get("includes_accessorials", True)
        total, n = ZERO, 0
        for iid, (doc, inv) in self.unique.items():
            c = w.contract_for_invoice(inv)
            if not c or c[1].get("contract_id") != cid:
                continue
            d = parse_date(inv.get("invoice_date"))
            if not d or not (start <= d <= end):
                continue
            s = ZERO
            lines = inv.get("lines") or []
            for ln in lines:
                code = w.code_of(ln, cf)
                if code in ("DUTY", "TAX", "LATE") or ln.get("late_fee"):
                    continue
                if not acc and code not in ("FRT", "FUEL", "SEC"):
                    continue
                s += D(ln.get("amount")) or ZERO
            if not lines:
                s = D(inv.get("total")) or ZERO
            u, _, miss = self.usd(cf, inv, s)
            if u is None:
                return None, n
            total += u
            n += 1
        for cdoc, cn in w.credit_notes:
            if cn.get("contract_id") not in (cid, None) or cn.get("credit_type") in ("rebate", "promised_credit",
                                                                                     "improvement", "margin_step_down"):
                continue
            d = parse_date(cn.get("date"))
            if d and start <= d <= end:
                total -= D(cn.get("amount")) or ZERO
        return total, n

    def credits_issued(self, cid, period, ctype):
        out, evs = ZERO, []
        for cdoc, cn in self.w.credit_notes:
            t = cn.get("credit_type")
            t = "promised_credit" if t in ("improvement", "promised_credit") else t
            if cn.get("contract_id") in (cid, None) and str(cn.get("period")) == period and t == ctype:
                out += D(cn.get("amount")) or ZERO
                evs.append(self.w.ev(cdoc, cn.get("amount_src") or cn.get("src")))
        return out, evs

    def cashback(self):
        w = self.w
        for cid, (cdoc, cf) in w.contracts.items():
            eff = parse_date(cf.get("effective_date"))
            if not eff:
                continue
            cl = cf.get("clauses") or {}
            rb = cf.get("rebate")
            if rb and rb.get("tiers"):
                q = quarter_of(eff)
                while True:
                    qs_, qe = quarter_bounds(q)
                    if qe >= w.as_of:
                        break
                    spend, n = self.qualifying(cid, cf, qs_, qe)
                    if spend is not None and n:
                        tier = None
                        for t in sorted(rb["tiers"], key=lambda t: D(t["min_usd"])):
                            if spend >= D(t["min_usd"]):
                                tier = t
                        if tier:
                            earned = pct_of(D(tier["pct"]), spend)
                            got, cev = self.credits_issued(cid, q, "rebate")
                            owed = earned - got
                            if owed > 0:
                                due = qe + days(int(rb.get("credit_days") or 45))
                                remedy = "claim_credit" if w.as_of > due else "track_credit"
                                ev = _evs(w.ev(cdoc, cl.get("rebate")), w.ev(cdoc, cl.get("qs")), cev)
                                it = self._mk(("cashback", cid, q, "rebate"), remedy, ev, (cdoc, cf), amount=r2(owed),
                                              conf=0.9)
                                it.update(contract_id=cid, period=q, credit_type="rebate")
                                self.add(it)
                    y, n_ = int(q[:4]), int(q[-1])
                    q = f"{y + (n_ == 4)}-Q{n_ % 4 + 1}"
            im = cf.get("improvement")
            if im and D(im.get("pct")):
                n = 1
                while True:
                    start = add_years(eff, n - 1)
                    end = add_years(eff, n) - days(1)
                    if end >= w.as_of:
                        break
                    spend, cnt = self.qualifying(cid, cf, start, end)
                    if spend is not None and cnt:
                        earned = pct_of(D(im["pct"]), spend)
                        got, cev = self.credits_issued(cid, f"CY{n}", "promised_credit")
                        owed = earned - got
                        if owed > 0:
                            due = add_years(eff, n) + days(int(im.get("due_days") or 60))
                            remedy = "claim_credit" if w.as_of > due else "track_credit"
                            ev = _evs(w.ev(cdoc, cl.get("improvement")), w.ev(cdoc, cl.get("qs")), cev)
                            it = self._mk(("cashback", cid, f"CY{n}", "promised_credit"), remedy, ev, (cdoc, cf),
                                          amount=r2(owed), conf=0.88)
                            it.update(contract_id=cid, period=f"CY{n}", credit_type="promised_credit")
                            self.add(it)
                    n += 1

    def margin_step_down(self):
        """A handling margin that should have stepped down: per quarter, every invoice after the one whose spend
        crossed the threshold should carry the lower margin. One cashback item per quarter, never per invoice."""
        w = self.w
        for cid, (cdoc, cf) in w.contracts.items():
            mg = cf.get("margin") or {}
            step, th = D(mg.get("step_down_pct")), D(mg.get("threshold_usd"))
            if step is None or th is None:
                continue
            quarters = defaultdict(list)
            for iid, (doc, inv) in self.unique.items():
                c = w.contract_for_invoice(inv)
                d = parse_date(inv.get("invoice_date"))
                if c and c[1].get("contract_id") == cid and d:
                    quarters[quarter_of(d)].append((d, doc.received_at, iid, doc, inv))
            cl = cf.get("clauses") or {}
            for q, invs in quarters.items():
                qs_, qe = quarter_bounds(q)
                if qe >= w.as_of:
                    continue
                invs.sort(key=lambda x: (x[0], x[1]))
                cum, crossed, owed, evs = ZERO, False, ZERO, []
                for d, _, iid, doc, inv in invs:
                    if crossed:
                        for ln in inv.get("lines") or []:
                            if w.code_of(ln, cf) == "MARGIN" and D(ln.get("base")) is not None:
                                correct = pct_of(step, D(ln["base"]))
                                billed = D(ln.get("amount")) or ZERO
                                if billed > correct:
                                    u, _, _ = self.usd(cf, inv, billed - correct)
                                    owed += u or ZERO
                                    evs.append(w.ev(doc, ln.get("src")))
                    spend = ZERO
                    for ln in inv.get("lines") or []:
                        code = w.code_of(ln, cf)
                        if mg.get("basis") == "cost":
                            if code == "COST":
                                spend += D(ln.get("amount")) or ZERO
                        elif code not in ("DUTY", "TAX", "LATE") and not ln.get("late_fee"):
                            spend += D(ln.get("amount")) or ZERO
                    u, _, _ = self.usd(cf, inv, spend)
                    cum += u or ZERO
                    if not crossed and cum >= th:
                        crossed = True
                        evs.append(w.ev(doc, inv.get("src")))
                got, cev = self.credits_issued(cid, q, "margin_step_down")
                owed -= got
                if owed > 0:
                    ev = _evs(w.ev(cdoc, cl.get("margin_step") or cl.get("margin")), evs, cev)
                    it = self._mk(("cashback", cid, q, "margin_step_down"), "claim_credit", ev, (cdoc, cf),
                                  amount=r2(owed), conf=0.85)
                    it.update(contract_id=cid, period=q, credit_type="margin_step_down")
                    self.add(it)

    # ---- expiry -----------------------------------------------------------------------------------------------------
    def expiry(self):
        w = self.w
        for cid, (cdoc, cf) in w.contracts.items():
            end = parse_date(cf.get("end_date"))
            evs = [w.ev(cdoc, (cf.get("clauses") or {}).get("term"))]
            for adoc, a in w.amendments:
                if a.get("contract_id") == cid and (a.get("term") or {}).get("end_date") and a.get("signed", True):
                    end = parse_date(a["term"]["end_date"])
                    evs = [w.ev(adoc, a["term"].get("src"))]
            if not end:
                continue
            op = end
            if cf.get("auto_renew"):
                if cf.get("notice_deadline"):
                    op = parse_date(cf["notice_deadline"])
                elif cf.get("notice_days"):
                    op = end - days(int(cf["notice_days"]))
                evs.append(w.ev(cdoc, (cf.get("clauses") or {}).get("renewal")))
            if 0 <= (op - w.as_of).days <= 60:
                it = self._mk(("expiry", cid), "alert", _evs(evs), (cdoc, cf), conf=0.95)
                it["deadline"] = op
                it["contract_id"] = cid
                self.add(it)

    # ---- building an item -------------------------------------------------------------------------------------------
    def _mk(self, key, remedy, evidence, contract, *, verdict="finding", amount=None, rng=None, missing=None,
            conf=0.9, gate=None, learned=None, amount_cur=None, conflict_lanes=None, deciding=None, variant=None):
        if missing and verdict == "finding":
            verdict, remedy = "cannot_determine", "request_document"
            amount = None
        return {"key": key, "verdict": verdict, "amount": amount, "range": rng, "deadline": None, "remedy": remedy,
                "evidence": [e for e in evidence if e], "missing": missing, "contract": contract,
                "confidence": conf, "gate": gate or {}, "learned": learned, "amount_cur": amount_cur,
                "conflict_lanes": conflict_lanes or set(), "deciding": deciding or [], "variant": variant}

    def run(self) -> dict[tuple, dict]:
        self.duplicates()
        self.waivers()
        self.overcharges()
        self.late_fees()
        self.cashback()
        self.margin_step_down()
        self.expiry()
        self.late_fee_risk()
        return self.items
