"""Repricing an invoice: what every line should have cost, from the highest source in force on the pickup date.

Rate for a freight line (lane, chargeable weight, pickup date), in order of precedence:
  1. a signed amendment (or an annex it brings in) in force on the pickup date; the latest effective date wins.
     An annex the amendment names but we don't hold -> the line can't be priced: cannot_determine.
  2. a rate notice from the carrier's Schedule 1 rates person, inside its window. Against an amendment for the same
     lane, the contract's unranked-instruments clause makes it a conflict (both sources go to a person).
     A notice accepted in feedback (rate_accepted) counts for its contract, lane and dates.
  3. the MSA's Annex A, in the rate period that holds the pickup date (the last period continues if the MSA says so).
Fuel is the Annex C percentage for the pickup month on the *correct* freight. Accessorials come from Annex D, with
the shipment's facts deciding whether they are owed (dry ice vs DG, storage days, delivery vs collection, customs
fault, peak season months).
"""
from __future__ import annotations

from decimal import Decimal

from .util import D, ZERO, norm_label, parse_date, r2, ym

PRICED = {"FRT", "FUEL", "SEC", "PEAK", "DG", "DRYICE", "STOR", "DLV", "CUSTOMS", "ACCT", "SPROG", "PPROG"}
DATE_DEPENDENT = {"FRT", "FUEL", "PEAK", "SEC"}


def _band_rows(rows, lane):
    return [r for r in rows if r.get("lane") == lane]


def pick_band(rows: list[dict], w: Decimal):
    """The weight band that contains w (the row with the greatest lower bound <= w), and the minimum-charge row."""
    best, minimum = None, None
    for r in rows:
        if r.get("minimum"):
            minimum = r
            continue
        lo = D(r.get("band_min")) or ZERO
        hi = D(r.get("band_max"))
        if lo <= w and (best is None or lo > (D(best.get("band_min")) or ZERO)):
            if hi is None or w <= hi or True:
                best = r
    return best, minimum


def _period(cf: dict, d):
    ps = sorted(cf.get("periods") or [], key=lambda p: p["from"])
    for p in ps:
        if parse_date(p["from"]) <= d <= parse_date(p["to"]):
            return p["name"]
    if ps and d > parse_date(ps[-1]["to"]) and cf.get("last_period_continues", True):
        return ps[-1]["name"]
    if ps and d < parse_date(ps[0]["from"]):
        return ps[0]["name"]
    return None


def _row_rate(row: dict, period: str | None):
    if row is None:
        return None
    if row.get("rates"):
        if period and period in row["rates"]:
            return D(row["rates"][period])
        return D(list(row["rates"].values())[-1])
    return D(row.get("rate"))


class RateSource:
    def __init__(self, kind, rate, minimum, evs, authority, eff=None, learned=None, name=None):
        self.kind, self.rate, self.minimum, self.evs = kind, rate, minimum, [e for e in evs if e]
        self.authority, self.eff, self.learned, self.name = authority, eff, learned, name

    def charge(self, w: Decimal) -> Decimal:
        amt = r2(self.rate * w)
        if self.minimum is not None and amt < self.minimum:
            return self.minimum
        return amt


def rate_for(world, cdoc, cf, lane, w, d, fb, key=None):
    """-> {"source": RateSource | None, "alt": RateSource | None (conflict), "missing": {...} | None}."""
    cid = cf.get("contract_id")
    out = {"source": None, "alt": None, "missing": None}
    cl = cf.get("clauses") or {}
    # 3. the MSA
    period = _period(cf, d)
    rows = _band_rows(cf.get("rates") or [], lane)
    msa = None
    if rows:
        band, mn = pick_band(rows, w)
        if band:
            msa = RateSource("msa", _row_rate(band, period), _row_rate(mn, period),
                             [world.ev(cdoc, band.get("src")), world.ev(cdoc, cl.get("pricing_date"))], 1, name=cid)
    # 1. amendments and their annexes
    amd = None
    for adoc, a in world.amendments:
        if a.get("contract_id") not in (cid, None) or a.get("signed") is False:
            continue
        eff = parse_date(a.get("effective_from"))
        if not eff or eff > d:
            continue
        arows = _band_rows(a.get("rates") or [], lane)
        if arows:
            band, mn = pick_band(arows, w)
            if band and (amd is None or eff >= amd.eff):
                amd = RateSource("amendment", _row_rate(band, None), _row_rate(mn, None) if mn else
                                 (msa.minimum if msa else None), [world.ev(adoc, band.get("src"))], 1, eff,
                                 name=a.get("amendment_ref"))
        for ref in a.get("annex_refs") or []:
            frm = parse_date(ref.get("from")) or eff
            if lane not in (ref.get("lanes") or []) or frm > d:
                continue
            held = None
            for ndoc, an in world.annexes:
                if norm_label(an.get("name")) == norm_label(ref.get("name")) and \
                        an.get("contract_id") in (None, cid) and _band_rows(an.get("rates") or [], lane):
                    held = (ndoc, an)
            if held:
                ndoc, an = held
                band, mn = pick_band(_band_rows(an["rates"], lane), w)
                if band and (amd is None or frm >= amd.eff):
                    amd = RateSource("annex", _row_rate(band, None), _row_rate(mn, None) if mn else
                                     (msa.minimum if msa else None),
                                     [world.ev(ndoc, band.get("src")), world.ev(adoc, ref.get("src"))], 1, frm,
                                     name=an.get("name"))
            elif amd is None or frm >= amd.eff:
                amd = RateSource("missing", None, None, [world.ev(adoc, ref.get("src"))], 1, frm, name=ref.get("name"))
    # 2. rate notices
    notice = None
    for ndoc, n in world.notices:
        if n.get("contract_id") not in (None, cid) or n.get("quoted"):
            continue                                   # quoted / forwarded text is not a notice
        nrows = _band_rows(n.get("rates") or [], lane)
        if not nrows:
            continue
        f, t = parse_date(n.get("effective_from")), parse_date(n.get("effective_to"))
        if f and d < f or t and d > t:
            continue
        ok, who, how = world.authorised(cf, ndoc, "rates", on=d, feedback=fb)
        learned = None
        if not ok and fb is not None:
            fid = fb.accepted_notice(cid, lane, d, ndoc, n)
            if fid:
                ok, learned = True, fid
        elif how.startswith("feedback:"):
            learned = how.split(":", 1)[1]
        if not ok:
            continue
        band, mn = pick_band(nrows, w)
        if band and (notice is None or (f or d) >= (notice.eff or d)):
            notice = RateSource("notice", _row_rate(band, None), _row_rate(mn, None) if mn else
                                (msa.minimum if msa else None), [world.ev(ndoc, band.get("src"))], 2, f or d,
                                learned=learned, name=n.get("reference"))
    # combine
    if amd is not None and amd.kind == "missing":
        out["missing"] = {"name": amd.name, "evs": amd.evs, "from": amd.eff}
        return out
    if amd is not None:
        if notice is not None and notice.rate != amd.rate:
            pick = fb.precedence(cid, lane, d, key) if fb is not None else None
            if pick:
                use, fid = pick
                src = notice if use == "notice" else amd
                src.learned = fid
                out["source"] = src
            elif cf.get("unranked_notice_vs_amendment", True):
                amd.evs.append(world.ev(cdoc, cl.get("unranked")))
                out["source"], out["alt"] = amd, notice
            else:
                out["source"] = amd
        else:
            out["source"] = amd
        return out
    if notice is not None:
        notice.evs.append(world.ev(cdoc, cl.get("rate_notice")))
        out["source"] = notice
        return out
    out["source"] = msa
    return out


def fuel_pct(world, cdoc, cf, d):
    for r in cf.get("fuel") or []:
        if r.get("month") == ym(d):
            return D(r["pct"]), world.ev(cdoc, r.get("src"))
    return None, None


def accessorial(cf, code):
    for a in cf.get("accessorials") or []:
        if a.get("code") == code:
            return a
    return None


def dg_profile(world, awb):
    """What the declaration / AWB says about dangerous goods -> (known, has_other_dg, has_dry_ice, evidence)."""
    evs, uns, dry = [], set(), False
    known = False
    if awb in world.dgds:
        doc, f = world.dgds[awb]
        known = True
        for it in f.get("items") or []:
            u = str(it.get("un") or "").replace("UN", "")
            if u == "1845" or "dry ice" in str(it.get("description", "")).lower():
                dry = True
            elif u:
                uns.add(u)
            evs.append(world.ev(doc, it.get("src")))
    if awb in world.awbs:
        doc, f = world.awbs[awb]
        for u in f.get("un_numbers") or []:
            known = True
            if u == "1845":
                dry = True
            else:
                uns.add(u)
        if f.get("dry_ice_kg"):
            known, dry = True, True
    return known, bool(uns), dry, [e for e in evs if e]


def price_invoice(world, inv_doc, inv: dict, fb, key=None) -> dict:
    """-> {"contract": (cdoc, cf) | None, "lines": [line result], "pickups": {awb: (date, source, ev)}}.
    A line result: {"line", "code", "billed", "correct", "alt", "status": ok|over|unknown|missing|conflict, "evs",
                    "date_dependent", "awb", "lane", "learned"}."""
    c = world.contract_for_invoice(inv)
    out = {"contract": c, "lines": [], "pickups": {}}
    if not c:
        return out
    cdoc, cf = c
    cl = cf.get("clauses") or {}
    lines = inv.get("lines") or []
    by_awb: dict = {}
    for ln in lines:
        by_awb.setdefault(ln.get("awb"), []).append(ln)
    freight: dict = {}       # awb -> (correct, alt, status)
    results = []
    for awb, group in by_awb.items():
        codes = [world.code_of(ln, cf) for ln in group]
        frt = next((ln for ln, cd in zip(group, codes) if cd == "FRT"), None)
        ref_line = frt or group[0]
        d, dsrc, dev = world.pickup(awb, ref_line, inv_doc, cf) if awb else (None, None, None)
        if awb:
            out["pickups"][awb] = (d, dsrc, dev)
        lane = next((ln.get("lane") for ln in group if ln.get("lane")), None)
        # chargeable weight: the AWB's when we hold it, else as printed on the freight / per-kg line
        w = None
        w_ev = None
        if awb and awb in world.awbs and D(world.awbs[awb][1].get("chargeable_weight")):
            adoc, af = world.awbs[awb]
            w, w_ev = D(af["chargeable_weight"]), world.ev(adoc, af.get("chargeable_src"))
        if w is None:
            for ln, cd in zip(group, codes):
                if cd in ("FRT", "SEC", "PEAK") and (ln.get("unit") or "kg") == "kg" and D(ln.get("qty")):
                    w = D(ln["qty"])
                    break
        prof = dg_profile(world, awb) if awb else (False, False, False, [])
        for ln, code in zip(group, codes):
            billed = D(ln.get("amount"))
            r = {"line": ln, "code": code, "billed": billed, "correct": None, "alt": None, "status": "ok",
                 "evs": [], "date_dependent": code in DATE_DEPENDENT, "awb": awb, "lane": lane, "learned": None,
                 "pickup": (d, dsrc, dev)}
            results.append(r)
            if billed is None or code not in PRICED or ln.get("late_fee"):
                continue
            if code == "FRT":
                if d is None or w is None or not lane:
                    r["status"] = "unknown"
                    continue
                rr = rate_for(world, cdoc, cf, lane, w, d, fb, key)
                if rr["missing"]:
                    r["status"] = "missing"
                    r["missing"] = rr["missing"]
                    freight[awb] = (None, None, "missing")
                    continue
                src = rr["source"]
                if src is None or src.rate is None:
                    r["status"] = "unknown"
                    freight[awb] = (None, None, "unknown")
                    continue
                r["correct"] = src.charge(w)
                r["evs"] = src.evs + [dev, w_ev if w_ev and D(ln.get("qty")) != w else None]
                r["source"] = src
                r["learned"] = src.learned
                if rr["alt"] is not None:
                    r["alt"] = rr["alt"].charge(w)
                    r["alt_source"] = rr["alt"]
                    r["evs"] += rr["alt"].evs
                    r["status"] = "conflict"
                freight[awb] = (r["correct"], r["alt"], r["status"])
            elif code == "FUEL":
                pass                                   # after the freight of the same AWB is known (below)
            elif code in ("SEC", "PEAK"):
                a = accessorial(cf, code)
                if not a or w is None:
                    continue
                fee = D(a["fee"])
                if code == "PEAK" and a.get("months") and d is not None and d.month not in a["months"]:
                    r["correct"] = ZERO
                else:
                    r["correct"] = r2(fee * w)
                r["evs"] = [world.ev(cdoc, a.get("src"))]
            elif code in ("DG", "DRYICE"):
                a = accessorial(cf, code)
                known, other_dg, dry, devs = prof
                if not a or not known:
                    continue
                fee = D(a["fee"])
                q = D(ln.get("qty")) or Decimal(1)
                if code == "DG":
                    r["correct"] = r2(fee * min(q, Decimal(1))) if other_dg else ZERO
                    r["evs"] = devs + [world.ev(cdoc, cl.get("dry_ice") if not other_dg else cl.get("dg"))]
                else:
                    r["correct"] = ZERO if other_dg or not dry else r2(fee * min(q, Decimal(1)))
                    r["evs"] = devs + [world.ev(cdoc, cl.get("mixed") if other_dg else cl.get("dry_ice"))]
            elif code == "STOR":
                a = accessorial(cf, code)
                arr, aev = world.milestone(awb, ("ARRIVED_FACILITY",)) if awb else (None, None)
                rel, rev = world.milestone(awb, ("DELIVERED", "COLLECTED_BY_CONSIGNEE")) if awb else (None, None)
                if not a or not arr or not rel:
                    continue
                days = max(0, (rel - arr).days - int(cf.get("storage_free_days") or 0))
                r["correct"] = r2(D(a["fee"]) * days)
                r["evs"] = [aev, rev, world.ev(cdoc, cl.get("storage"))]
            elif code == "DLV":
                a = accessorial(cf, code)
                if not a:
                    continue
                col, cev = world.milestone(awb, ("COLLECTED_BY_CONSIGNEE",)) if awb else (None, None)
                dlv, _ = world.milestone(awb, ("DELIVERED",)) if awb else (None, None)
                if col and not dlv:
                    r["correct"] = ZERO
                    r["evs"] = [cev, world.ev(cdoc, cl.get("delivery"))]
                else:
                    r["correct"] = r2(D(a["fee"]) * min(D(ln.get("qty")) or Decimal(1), Decimal(1)))
                    r["evs"] = [world.ev(cdoc, a.get("src"))]
            elif code == "CUSTOMS":
                a = accessorial(cf, code)
                if not a:
                    continue
                fault = None
                cev = None
                if awb in world.customs:
                    cdoc2, cf2 = world.customs[awb]
                    fault, cev = cf2.get("fault"), world.ev(cdoc2, cf2.get("src"))
                for e, dd, edoc, row, src in world.events.get(awb, []):
                    if e == "CUSTOMS_HOLD" and row.get("reason") and fault in (None, "unknown"):
                        rs = str(row["reason"]).upper()
                        fault = "carrier" if "CARRIER" in rs else "shipper" if "SHIPPER" in rs else fault
                        cev = cev or world.ev(edoc, row.get("src"))
                r["correct"] = ZERO if fault == "carrier" else D(a["fee"])
                r["evs"] = [cev, world.ev(cdoc, cl.get("customs"))]
            elif code in ("ACCT", "SPROG", "PPROG") or accessorial(cf, code):
                a = accessorial(cf, code)
                if not a:
                    continue
                q = D(ln.get("qty")) or Decimal(1)
                r["correct"] = r2(D(a["fee"]) * q)
                r["evs"] = [world.ev(cdoc, a.get("src"))]
        # fuel rides on the correct freight of the same shipment
        for r in results:
            if r["awb"] != awb or r["code"] != "FUEL" or r["billed"] is None:
                continue
            fc, falt, fst = freight.get(awb, (None, None, "unknown"))
            if fst in ("missing",):
                r["status"] = "missing"
                continue
            if fc is None or d is None:
                r["status"] = "unknown"
                continue
            pct, pev = fuel_pct(world, cdoc, cf, d)
            if pct is None:
                continue
            r["correct"] = r2(pct / 100 * fc)
            if falt is not None:
                r["alt"] = r2(pct / 100 * falt)
                r["status"] = "conflict"
            r["evs"] = [pev, world.ev(cdoc, cl.get("fuel"))]
    for r in results:
        if r["correct"] is not None and r["status"] == "ok" and r["billed"] is not None and r["billed"] > r["correct"]:
            r["status"] = "over"
    out["lines"] = results
    return out
