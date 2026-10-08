"""A person's decisions (feedback.json), stored with their source and applied no further than their scope.

Each item becomes a scoped fact. A `standing` fact applies to a later key only when every filter in `applies_to`
matches and the shipment / document date is inside valid_from..valid_to. A `one_off` decision changes its own key
and nothing else. Every item that changed because of feedback carries learned_at {feedback_item_id, batch}.

    confirm + sender_authority      the person holds those categories for that contract from valid_from
    close + dispute_closed          stop raising that key (a new invoice with the same error is a new key)
    close + rate_accepted           stop raising that key; the named notice is the rate for that contract + lane
    reject                          that key is not a finding
    one_off_approval + waiver_scope the waiver covers the named months, on that key only
    resolve_conflict + precedence   use the named source for that contract + lane from valid_from
    override_amount                 use fact.value.amount_usd for that key
"""
from __future__ import annotations

from .util import norm_invoice, norm_person, parse_date


def key_of(fk: dict) -> tuple:
    if "period" in fk:
        return ("cashback", str(fk.get("contract_id")), str(fk.get("period")), str(fk.get("credit_type")))
    if fk.get("class") in (None, "expiry") and "invoice_id" not in fk:
        return ("expiry", str(fk.get("contract_id")))
    return (fk.get("class"), norm_invoice(fk.get("invoice_id")))


class Feedback:
    def __init__(self, files: list[dict]):
        self.items = []
        for fb in files:
            for it in fb.get("items") or []:
                it = dict(it)
                it["_batch"] = fb.get("after_batch")
                it["_decided_at"] = parse_date(fb.get("decided_at"))
                it["_key"] = key_of(it.get("finding_key") or {})
                self.items.append(it)

    def learned(self, it: dict) -> dict:
        return {"feedback_item_id": str(it.get("item_id")), "batch": str(it.get("_batch"))}

    @staticmethod
    def _in_scope(it: dict, d) -> bool:
        sc = it.get("scope") or {}
        f, t = parse_date(sc.get("valid_from")), parse_date(sc.get("valid_to"))
        if d is None:
            return True
        return (f is None or d >= f) and (t is None or d <= t)

    def by_key(self, key: tuple) -> list[dict]:
        return [it for it in self.items if it["_key"] == key]

    # ---- per-key decisions ------------------------------------------------------------------------------------------
    def suppressed(self, key: tuple):
        """close / reject on exactly this key -> the item, else None."""
        for it in reversed(self.by_key(key)):
            if it.get("decision") in ("close", "reject"):
                return it
        return None

    def override(self, key: tuple):
        for it in reversed(self.by_key(key)):
            if it.get("decision") == "override_amount":
                v = ((it.get("fact") or {}).get("value") or {}).get("amount_usd")
                if v is not None:
                    return it, v
        return None

    def waiver_months(self, key: tuple):
        """one_off_approval + waiver_scope on exactly this key -> (item, months)."""
        for it in reversed(self.by_key(key)):
            f = it.get("fact") or {}
            if it.get("decision") in ("one_off_approval", "confirm") and f.get("type") == "waiver_scope":
                return it, list((f.get("value") or {}).get("months") or [])
        return None

    # ---- standing facts ---------------------------------------------------------------------------------------------
    def authority(self, contract_id, name, email, category, on) -> str | None:
        """confirm + sender_authority: does this person hold this category on this contract on this date?"""
        for it in self.items:
            f = it.get("fact") or {}
            if f.get("type") != "sender_authority" or it.get("decision") not in ("confirm", "one_off_approval"):
                continue
            cats = (f.get("value") or {}).get("categories") or []
            if category not in cats:
                continue
            sc = it.get("scope") or {}
            ap = sc.get("applies_to") or {}
            person = ap.get("person") or (f.get("subject") or {}).get("person")
            if norm_person(person) != norm_person(name) and str(person or "").lower() != str(email or "").lower():
                continue
            if ap.get("contract_id") and ap["contract_id"] != contract_id:
                continue
            if ap.get("category") and ap["category"] != category:
                continue
            if sc.get("kind") == "one_off":
                continue
            if self._in_scope(it, on):
                return str(it.get("item_id"))
        return None

    def accepted_notice(self, contract_id, lane, d, ndoc, notice) -> str | None:
        """close + rate_accepted: the named notice is the rate for its contract and lane inside the dates."""
        for it in self.items:
            f = it.get("fact") or {}
            if f.get("type") != "rate_accepted":
                continue
            sc = it.get("scope") or {}
            ap = sc.get("applies_to") or {}
            subj = f.get("subject") or {}
            if (ap.get("contract_id") or subj.get("contract_id")) not in (None, contract_id):
                continue
            if (ap.get("lane") or subj.get("lane")) not in (None, lane):
                continue
            if not self._in_scope(it, d):
                continue
            v = f.get("value") or {}
            sent_by = v.get("sent_by")
            if sent_by and norm_person(sent_by) != norm_person((ndoc.meta or {}).get("from_name")):
                continue
            sent_on = parse_date(v.get("sent_on"))
            if sent_on and ndoc.received != sent_on:
                continue
            return str(it.get("item_id"))
        return None

    def precedence(self, contract_id, lane, d, key=None):
        """resolve_conflict + precedence -> ('amendment' | 'notice', item id) or None. A later signed amendment still
        beats it (the caller only asks when an amendment and a notice disagree)."""
        for it in reversed(self.items):
            f = it.get("fact") or {}
            if it.get("decision") != "resolve_conflict" and f.get("type") != "precedence":
                continue
            sc = it.get("scope") or {}
            ap = sc.get("applies_to") or {}
            subj = f.get("subject") or {}
            if (ap.get("contract_id") or subj.get("contract_id")) not in (None, contract_id):
                continue
            if (ap.get("lane") or subj.get("lane")) not in (None, lane):
                continue
            own = key is not None and it["_key"] == key
            if not own and not self._in_scope(it, d):
                continue
            use = str((f.get("value") or {}).get("use") or "").lower()
            return ("notice" if "notice" in use or "correspond" in use else "amendment"), str(it.get("item_id"))
        return None
