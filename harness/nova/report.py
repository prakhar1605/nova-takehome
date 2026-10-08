"""From candidate items to report.json: feedback applied, actions chosen per mode, ids kept stable across reports,
`supersedes` / `retro` / `learned_at` set, confidence attached.

Actions (GLOSSARY "Actions" and "The two modes"):
    internal remedies (alert, payment_reminder, track_credit) run in both modes; conflicts escalate in both modes.
    outbound remedies (dispute, claim_credit, request_document):
        $5,000.00 or more on one invoice / cashback key (summed over its outbound findings) -> human_review
        supervised -> draft:<remedy>
        autonomous -> execute:<remedy> only when the unattended-action test passes, else human_review
"""
from __future__ import annotations

from decimal import Decimal

from .checks import Checks
from .feedback import Feedback
from .util import ZERO, money, norm_invoice, parse_date
from .world import World

THRESHOLD = Decimal("5000.00")
OUTBOUND = {"dispute", "claim_credit", "request_document"}
INTERNAL = {"alert": "internal:alert", "payment_reminder": "internal:payment_reminder",
            "track_credit": "internal:track_credit"}


def key_str(k: tuple) -> str:
    return "|".join(str(x) for x in k)


def base_id(k: tuple) -> str:
    if k[0] == "cashback":
        return f"cashback:{k[1]}/{k[2]}/{k[3]}"
    return f"{k[0]}:{k[1]}"


def _state(it: dict):
    return (it["verdict"], money(it["amount"]) if it.get("amount") is not None else None,
            tuple(money(x) for x in it["range"]) if it.get("range") else None,
            str(it.get("deadline")) if it.get("deadline") else None)


def unattended_ok(it: dict, items: dict, outbound_sum: Decimal, w) -> tuple[bool, list[str]]:
    """The five checks of the unattended-action test. -> (pass, reasons it failed)."""
    why = []
    g = it.get("gate") or {}
    if it.get("missing") and it["remedy"] != "request_document":
        why.append("a document is missing")
    if g.get("pickup_record"):
        why.append("pickup date not from tracking or a carrier pickup email")
    if g.get("conflict") or it["verdict"] == "conflict":
        why.append("conflict on this key")
    cf = (it.get("contract") or (None, {}))[1] or {}
    lanes = g.get("lanes") or set()
    for k2, o in items.items():
        if o is it or o["verdict"] != "conflict":
            continue
        ocf = (o.get("contract") or (None, {}))[1] or {}
        if ocf.get("contract_id") == cf.get("contract_id") and (o.get("conflict_lanes") or set()) & lanes:
            why.append(f"open conflict on the same lane ({base_id(k2)})")
    if it["remedy"] != "request_document" and outbound_sum >= THRESHOLD:
        why.append("$5,000 or more on this invoice")
    if it["remedy"] == "request_document":
        h = (it.get("missing") or {}).get("holder") or {}
        if not h.get("person"):
            why.append("no Schedule 1 holder to ask")
    elif w.billing_contact(cf) is None:
        why.append("no Schedule 1 billing-disputes contact")
    return not why, why


def build(store, as_of: str, mode: str) -> dict:
    w = World(store, as_of)
    fb = Feedback(store.feedback())
    items = Checks(w, fb).run()

    # ---- feedback on exact keys: close / reject / override ----
    for k in list(items):
        it = items[k]
        if fb.suppressed(k):
            del items[k]
            continue
        ov = fb.override(k)
        if ov and it["verdict"] in ("finding", "conflict"):
            fit, amt = ov
            it.update(verdict="finding", amount=Decimal(str(amt)), range=None, learned=str(fit.get("item_id")),
                      remedy="dispute" if it["remedy"] == "escalate" else it["remedy"])

    # ---- outbound totals per invoice / cashback key ----
    outbound: dict[str, Decimal] = {}
    for k, it in items.items():
        if it["verdict"] == "finding" and it["remedy"] in OUTBOUND and it.get("amount") is not None:
            sk = k[1] if k[0] != "cashback" else key_str(k)
            outbound[sk] = outbound.get(sk, ZERO) + it["amount"]

    # ---- history: ids, supersedes, retro ----
    hist = store.history()
    prev = None
    for h in hist:
        if h["mode"] == mode and h["as_of"] < as_of and (prev is None or h["as_of"] > prev["as_of"]):
            prev = h
    prev_items = (prev or {}).get("items", {})

    out = {"schema_version": "1.0", "as_of": as_of, "mode": mode, "findings": [],
           "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []}}
    record = {}
    for k in sorted(items, key=key_str):
        it = items[k]
        ks = key_str(k)
        cls = k[0]
        st = _state(it)
        pid = prev_items.get(ks)
        if pid and tuple(pid["state"]) == tuple(st):
            iid, sup = pid["id"], []
        elif pid:
            iid, sup = f"{base_id(k)}@{as_of}", [pid["id"]]
        else:
            iid, sup = base_id(k), []
        retro = False
        if prev and not pid and it.get("deciding"):
            pas = parse_date(prev["as_of"])
            inv = w.invoices.get(k[1]) if len(k) == 2 else None
            if inv and inv[0][0].received <= pas:
                for src in it["deciding"]:
                    for e in (getattr(src, "evs", None) or []):
                        d = w.docs.get(e["doc"])
                        if d and d.received > pas and getattr(src, "kind", "") in ("amendment", "annex", "notice"):
                            retro = True
        # action
        remedy = it["remedy"]
        sk = k[1] if cls != "cashback" else ks
        if it["verdict"] == "conflict":
            action = "escalate"
        elif remedy in INTERNAL:
            action = INTERNAL[remedy]
        elif remedy in OUTBOUND:
            tot = outbound.get(sk, ZERO)
            if it["verdict"] == "finding" and tot >= THRESHOLD:
                action = "human_review"
            elif mode == "supervised":
                action = f"draft:{remedy}"
            else:
                ok, why = unattended_ok(it, items, tot, w)
                action = f"execute:{remedy}" if ok else "human_review"
                if not ok:
                    it["held_back"] = why
        else:
            action = "escalate"
        cf = (it.get("contract") or (None, {}))[1] or {}
        row = {"id": iid, "class": "cashback" if cls == "cashback" else cls}
        if cls == "cashback":
            row.update(contract_id=k[1], period=k[2], credit_type=k[3])
        elif cls == "expiry":
            row.update(contract_id=k[1])
        else:
            row["invoice_id"] = k[1]
            if cf.get("contract_id"):
                row["contract_id"] = cf["contract_id"]
        row.update({
            "verdict": it["verdict"],
            "amount_usd": money(it["amount"]) if it["verdict"] == "finding" and it.get("amount") is not None else None,
            "amount_range": [money(x) for x in it["range"]] if it["verdict"] == "conflict" and it.get("range") else None,
            "deadline": it["deadline"].isoformat() if it.get("deadline") else None,
            "action": action,
            "confidence": round(float(it.get("confidence") or 0.9), 3),
            "evidence": it["evidence"],
            "missing_doc": it.get("missing") if it["verdict"] == "cannot_determine" else None,
            "supersedes": sup,
            "retro": retro,
            "learned_at": None,
        })
        if it.get("learned"):
            fit = next((x for x in fb.items if str(x.get("item_id")) == str(it["learned"])), None)
            if fit:
                row["learned_at"] = fb.learned(fit)
        if action.startswith(("draft:dispute", "execute:dispute", "draft:claim", "execute:claim")) or \
                (action == "human_review" and remedy in ("dispute", "claim_credit")):
            bc = w.billing_contact(cf)
            if bc:
                row["recipient"] = bc
        elif "request_document" in action or (action == "human_review" and remedy == "request_document"):
            h = (it.get("missing") or {}).get("holder") or {}
            p = w.person(cf, name=h.get("person")) if h.get("person") else None
            if p:
                row["recipient"] = {"name": p["name"], "email": p.get("email"), "role": p.get("title")}
        if it.get("held_back"):
            row["held_back"] = it["held_back"]
        if row["evidence"] == [] and it["verdict"] in ("finding", "conflict", "cannot_determine"):
            continue                                       # nothing we can cite: don't claim it
        if cls in ("overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed"):
            out["findings"].append(row)
        elif cls == "cashback":
            out["portfolio"]["cashback"].append(row)
        elif cls == "expiry":
            out["portfolio"]["expiry"].append(row)
        elif cls == "late_fee_risk":
            out["portfolio"]["late_fee_risk"].append(row)
        record[ks] = {"id": iid, "state": list(st)}

    hist = [h for h in hist if not (h["mode"] == mode and h["as_of"] == as_of)]
    hist.append({"mode": mode, "as_of": as_of, "items": record})
    store.save_history(hist)
    return out
