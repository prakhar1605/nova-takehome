"""Compare what the LLM reader and the rules reader read from every document in a state folder.

    python tools/compare_readers.py <state_dir> [--all]

Ingest stores both readings (facts and rules_facts). Agreement on a field is evidence the field is right; a
disagreement is printed so a person (or the next eval case) can look at it. Used to tune both readers.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
from nova.store import Store  # noqa: E402
from nova.util import D, norm_invoice, norm_label  # noqa: E402


def n(x):
    d = D(x)
    return str(d.normalize()) if d is not None else (str(x) if x not in (None, "") else None)


def inv_key(f):
    return {"id": norm_invoice(f.get("invoice_id")), "date": f.get("invoice_date"), "due": f.get("due_date"),
            "total": n(f.get("total")), "cur": (f.get("currency") or "USD").upper(),
            "lines": sorted((tuple(ln.get("src") or ())[:2], ln.get("awb"), n(ln.get("amount")), n(ln.get("qty")),
                             n(ln.get("rate")), n(ln.get("pct")), n(ln.get("base")), ln.get("pickup_date"),
                             (ln.get("late_fee") or {}).get("invoice_id")) for ln in f.get("lines") or [])}


def contract_key(f):
    out = {k: f.get(k) for k in ("contract_id", "carrier_party", "effective_date", "end_date", "auto_renew",
                                 "payment_days", "storage_free_days", "last_period_continues",
                                 "unranked_notice_vs_amendment")}
    lf = f.get("late_fee") or {}
    out["late_fee"] = (n(lf.get("pct")), lf.get("grace_days"), lf.get("compounding"))
    out["rates"] = sorted((r["lane"], bool(r.get("minimum")), n(r.get("band_min")), n(r.get("band_max")),
                           tuple(sorted((k, n(v)) for k, v in (r.get("rates") or {}).items())) or n(r.get("rate")))
                          for r in f.get("rates") or [])
    out["fuel"] = sorted((r["month"], n(r["pct"])) for r in f.get("fuel") or [])
    out["fx"] = sorted((r["month"], n(r["rate"])) for r in f.get("fx") or [])
    out["acc"] = sorted((a["code"], n(a["fee"]), a.get("unit")) for a in f.get("accessorials") or [])
    out["s1"] = sorted((p["name"], p["party"], tuple(sorted(p.get("categories") or [])), p.get("email"))
                       for p in f.get("schedule1") or [])
    rb = f.get("rebate") or {}
    out["rebate"] = sorted((n(t["min_usd"]), n(t["pct"])) for t in rb.get("tiers") or [])
    out["improvement"] = n((f.get("improvement") or {}).get("pct"))
    return out


def simple(f):
    t = f["type"]
    if t == "invoice":
        return inv_key(f)
    if t == "contract":
        return contract_key(f)
    if t == "tracking":
        return sorted((e["awb"], e["event"], e["date"]) for e in f.get("events") or [])
    if t == "ledger":
        return sorted((norm_invoice(r["invoice_id"]), r.get("due_date"), n(r.get("amount")), r.get("status"),
                       r.get("paid_date")) for r in f.get("rows") or [])
    if t == "waiver":
        return (norm_label(f.get("charge_label") or f.get("charge_code")), sorted(f.get("awbs") or []),
                sorted(f.get("months") or []), bool(f.get("quoted")))
    if t == "awb":
        return (f.get("awb"), f.get("date"), n(f.get("chargeable_weight")), n(f.get("total_chargeable_weight")))
    if t == "dg_declaration":
        return sorted((str(i.get("un")).replace("UN", ""), bool(i.get("refrigerant_only"))) for i in f.get("items") or [])
    if t == "credit_note":
        return (f.get("credit_note_id"), f.get("credit_type"), f.get("period"), n(f.get("amount")))
    if t == "invoice_cover":
        return (sorted(norm_invoice(x) for x in f.get("invoice_ids") or []), bool(f.get("asks_payment")),
                bool(f.get("copy")))
    if t in ("amendment", "rate_notice", "annex"):
        return (f.get("effective_from"), f.get("effective_to"),
                sorted((r["lane"], bool(r.get("minimum")), n(r.get("band_min")), n(r.get("rate"))) for r in f.get("rates") or []),
                sorted((a.get("name"), tuple(a.get("lanes") or [])) for a in f.get("annex_refs") or []))
    if t == "due_date_extension":
        return (norm_invoice(f.get("invoice_id")), f.get("new_due_date"))
    return {k: v for k, v in f.items() if k not in ("src", "type")}


def main():
    st = Store(sys.argv[1])
    show_all = "--all" in sys.argv
    agree = differ = 0
    for d in st.docs():
        fx = st.facts(d["id"]) or {}
        if fx.get("reader") != "llm":
            continue
        a = {}
        for f in fx.get("facts") or []:
            a.setdefault(f["type"], []).append(simple(f))
        b = {}
        for f in fx.get("rules_facts") or []:
            if f.get("type") not in (None, "error"):
                b.setdefault(f["type"], []).append(simple(f))
        for t in sorted(set(a) | set(b)):
            if t == "injection":
                continue
            if json.dumps(a.get(t), sort_keys=True, default=str) == json.dumps(b.get(t), sort_keys=True, default=str):
                agree += 1
                continue
            differ += 1
            print(f"== {d['id']} [{t}]")
            if isinstance(a.get(t, [None])[0], dict) and isinstance(b.get(t, [None])[0], dict):
                for k in sorted(set(a[t][0]) | set(b[t][0])):
                    va, vb = a[t][0].get(k), b[t][0].get(k)
                    if va != vb:
                        if isinstance(va, list) and isinstance(vb, list):
                            print(f"   {k}: llm-only {[x for x in va if x not in vb][:6]}  rules-only "
                                  f"{[x for x in vb if x not in va][:6]}")
                        else:
                            print(f"   {k}: llm {va!r} | rules {vb!r}")
            else:
                print(f"   llm   {json.dumps(a.get(t), default=str)[:600]}")
                print(f"   rules {json.dumps(b.get(t), default=str)[:600]}")
    print(f"\n{agree} fact groups agree, {differ} differ")


if __name__ == "__main__":
    main()
