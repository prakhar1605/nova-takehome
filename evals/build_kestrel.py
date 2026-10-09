"""Build the Kestrel eval world: a forwarding contract with a handling margin on carrier cost, and a margin that steps
down once the quarter's carrier cost reaches a threshold. Nothing in the practice or exam data has a margin.

    python evals/build_kestrel.py      # writes evals/mailboxes/kestrel/ and two cases

Q2 2041: carrier cost 20,000 + 10,000 reaches the 30,000 threshold on the second invoice, so the third and fourth
invoices should carry 8% instead of 12%: (5,000 + 4,000) x 4% = 360.00 owed as one margin_step_down item.
Q3: one invoice bills 15% where the contract allows 12%: 3,000 x 3% = 90.00, a plain overcharge.
"""
from __future__ import annotations

import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import yaml

from build_velloran import eml, pdf, r2

ROOT = Path(__file__).resolve().parent
BOX = ROOT / "mailboxes" / "kestrel"
C = "KTR-US-FWD-03"
CDOM, SDOM = "kestrelfwd.example", "orrin.example"
AP = f"lena.marsh@{SDOM}"


def contract():
    p1 = ["Master Services Agreement (Forwarding)", f"Agreement No. {C}", "between", "Orrin Diagnostics Inc.",
          "(the Shipper)", "and", "Kestrel Forwarding Inc.", "(the Carrier)", "Dated 2041-03-20"]
    p2 = ["1 Definitions",
          "1.1 Qualifying Spend. Qualifying Spend means carrier cost and handling margin on invoices dated in the period. It",
          "excludes taxes, duties and late fees.",
          "2 Term",
          "2.1 Effective date. This Agreement takes effect on 2041-04-01 (the Effective Date).",
          "2.2 Initial term. The initial term is 12 months, ending on 2042-03-31.",
          "2.3 Auto-renewal. This Agreement does not renew automatically.",
          "3 Rates and charges",
          "3.1 Rates. Carrier cost is passed through at cost as invoiced by the operating carrier.",
          "7 Volume rebate",
          "7.1 Volume rebate. Not applicable to this Agreement.",
          "8 Handling margin",
          "8.1 Handling margin. The Carrier may bill a handling margin of 12% of the carrier cost on each invoice.",
          "8.2 Margin step-down. Once the carrier cost billed on invoices dated in a Quarter reaches USD 30,000.00, the",
          "handling margin on every later invoice dated in that Quarter is 8% of the carrier cost.",
          "9 Annual improvement credit",
          "9.1 Annual improvement credit. Not applicable to this Agreement.",
          "10 Payment",
          "10.1 Payment terms. Invoices are due 30 days after the invoice date.",
          "10.2 Late fee. Undisputed amounts unpaid more than 0 days after the due date bear a late fee of 1.50% per month.",
          "11 Waivers",
          "11.1 Waiver authority. A charge may be waived only in writing by a person listed in Schedule 1 for waivers."]
    p3 = ["Annex E: Charge codes (the Carrier may print any listed label for the code)",
          "Code COST: Carrier cost; also billed as: Linehaul cost pass-through",
          "Code MARGIN: Handling margin; also billed as: Forwarding margin",
          "Schedule 1: Authorised Persons (name, title, party: categories, notice address)",
          f"Bram Toller, Key Account Manager, KTR-US: notices <bram.toller@{CDOM}>",
          f"Ines Faro, Pricing Manager, KTR-US: rates <ines.faro@{CDOM}>",
          f"Ottilie Crane, Customer Finance Lead, KTR-US: waivers, payment_terms <ottilie.crane@{CDOM}>",
          f"Pim Achter, Billing Disputes Lead, KTR-US: billing_disputes <pim.achter@{CDOM}>",
          f"Lena Marsh, AP Manager, ORD-US: notices, payment_terms, billing_disputes <{AP}>",
          "Signed for and on behalf of the Shipper: Rhea Dunmore, CFO, 2041-03-20",
          "Signed for and on behalf of the Carrier: Abel Stroud, VP Sales, 2041-03-20"]
    return pdf([p1, p2, p3])


def invoice(no, d: date, awb, cost: Decimal, pct: Decimal):
    margin = r2(cost * pct / 100)
    rows = ["Kestrel Forwarding Inc.", f"INVOICE {no}", f"Invoice date {d.isoformat()} Due date "
            f"{date.fromordinal(d.toordinal() + 30).isoformat()}", f"Contract reference {C}",
            "No. AWB Description Qty / basis Rate Amount (USD)",
            f"1 {awb} Linehaul cost pass-through 1 shpt {cost:.2f} {cost:.2f}",
            f"2 {awb} Forwarding margin {cost:.2f} {pct:.2f}% {margin:.2f}",
            f"Total USD {cost + margin:.2f}"]
    return pdf([rows]), margin


def build():
    if BOX.exists():
        shutil.rmtree(BOX)
    (BOX / "batch_01").mkdir(parents=True)
    k = Path(BOX / "batch_01")
    mails = [eml(("Bram Toller", f"bram.toller@{CDOM}"), AP, datetime(2041, 3, 21, 9), f"Executed agreement {C}",
                 f"Hello Lena,\n\nThe executed agreement {C} is attached.\n\nBram Toller | Key Account Manager",
                 [(f"MSA-{C}.pdf", contract())])]
    plan = [("KF-2041-101", date(2041, 4, 10), "KF10000001", "20000.00", "12"),
            ("KF-2041-102", date(2041, 5, 8), "KF10000002", "10000.00", "12"),
            ("KF-2041-103", date(2041, 5, 29), "KF10000003", "5000.00", "12"),
            ("KF-2041-104", date(2041, 6, 20), "KF10000004", "4000.00", "12"),
            ("KF-2041-105", date(2041, 7, 10), "KF10000005", "3000.00", "15")]
    for no, d, awb, cost, pct in plan:
        data, _ = invoice(no, d, awb, Decimal(cost), Decimal(pct))
        mails.append(eml(("Kestrel Billing", f"billing@{CDOM}"), AP, datetime(d.year, d.month, d.day, 12),
                         f"Invoice {no}", f"Hello Lena,\n\nInvoice {no} is attached.\nPlease pay by the due date.\n\n"
                         "Kestrel Billing", [(f"{no}.pdf", data)]))
    for n, m in enumerate(mails, 1):
        (k / f"{n:04d}.eml").write_bytes(m)
    step = (Decimal("5000") + Decimal("4000")) * Decimal("0.04")
    over = Decimal("3000") * Decimal("0.03")
    cases = {
        "kes_margin_step_down": {"name": "handling margin not stepped down after the quarter's cost reached the "
                                          "threshold (one item for the quarter)",
                                  "mailbox": "evals/mailboxes/kestrel", "as_of": "2041-07-31", "mode": "supervised",
                                  "expect": {"key": {"contract_id": C, "period": "2041-Q2",
                                                     "credit_type": "margin_step_down"},
                                             "verdict": "finding", "amount_usd": f"{r2(step):.2f}",
                                             "action": "draft:claim_credit"},
                                  "why": "invoices 103 and 104 come after the one that crossed 30,000: 4% of 9,000"},
        "kes_margin_overcharge": {"name": "handling margin billed above the contract percentage",
                                  "mailbox": "evals/mailboxes/kestrel", "as_of": "2041-07-31", "mode": "supervised",
                                  "expect": {"key": {"invoice_id": "KF-2041-105", "class": "overcharge"},
                                             "verdict": "finding", "amount_usd": f"{r2(over):.2f}",
                                             "action": "draft:dispute"},
                                  "why": "15% billed where clause 8.1 allows 12%: 3% of 3,000"},
        "kes_stepped_invoice_not_overcharge": {"name": "an invoice owed the step-down is not also an overcharge",
                                  "mailbox": "evals/mailboxes/kestrel", "as_of": "2041-07-31", "mode": "supervised",
                                  "expect": {"key": {"invoice_id": "KF-2041-103", "class": "overcharge"},
                                             "verdict": "no_finding"},
                                  "why": "a margin that should have stepped down is a cashback item, never split "
                                         "into per-invoice overcharges"},
    }
    for name, c in cases.items():
        (ROOT / "cases" / f"{name}.yaml").write_text(yaml.safe_dump(c, sort_keys=False, width=120))
    print("built", BOX, "step-down", step, "overcharge", over)


if __name__ == "__main__":
    build()
