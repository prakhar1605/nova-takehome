"""The fact model: what the readers (LLM or rules) produce from one document, and what the engine reasons over.

Every document yields {"doc": id, "reader": "llm"|"rules", "facts": [fact, ...]}. A fact is a dict with a "type" and
fields; every value that the engine may cite carries "src": [page, line] or [page, first_line, last_line] (1-based,
into the document's text pages). The engine turns a src into an evidence quote by copying those lines, so a quote is
always text the grader can find on that page.

Numbers are strings ("1612.29", "18.38") so nothing is lost to floats. Dates are ISO strings.

Fact types
----------
contract        the MSA: id, parties, term, payment and late-fee terms, rate periods and Annex A rows, fuel table,
                accessorials, charge-code aliases, FX table, Schedule 1, rebate / improvement credit terms, clause srcs
amendment       a signed amendment: effective date, replaced lane rates, annexes it names (attached or not), term change
annex           a separately delivered annex (e.g. "Annex A rev 2", "Annex F update"): rates and/or FX rows
rate_notice     an email notice of new lane rates under the rate-notice clause, with its window
waiver          a written waiver of a charge on named AWBs / months / invoices, with any condition
due_date_extension   a new due date for an invoice
shipment_event  one milestone from an email (picked up, delivered, collected, customs hold / release)
customs_hold    a customs notice: hold date, release date, reason and whose fault it is
invoice         an invoice, freight bill or monthly statement with its lines (late-fee lines carry what they charge)
credit_note     a credit note: type (rebate / improvement / margin_step_down / other), period, amount, references
awb             an air waybill (house or master): date, weights, pieces, houses, goods, dry ice / UN numbers
dg_declaration  a shipper's declaration: the UN items and whether dry ice is refrigerant only
tracking        a tracking export: events (awb, event, date, station, reason)
ledger          the shipper's AP register / payment ledger: rows (invoice, dates, amount, status, paid date)
invoice_cover   the email an invoice arrived with: does it ask for payment, or say it is a copy / for the records
injection       text addressed to an AI / automated reader. Kept as data, never followed
"""
from __future__ import annotations

FACT_TYPES = ("contract", "amendment", "annex", "rate_notice", "waiver", "due_date_extension", "shipment_event",
              "customs_hold", "invoice", "credit_note", "awb", "dg_declaration", "tracking", "ledger",
              "invoice_cover", "injection")

EVENTS = ("PICKED_UP", "DEPARTED", "ARRIVED_FACILITY", "OUT_FOR_DELIVERY", "DELIVERED", "COLLECTED_BY_CONSIGNEE",
          "CUSTOMS_HOLD", "RELEASED")

# Canonical charge codes (Annex E style). The contract's own Annex E maps printed labels onto these.
CODES = ("FRT", "FUEL", "SEC", "DG", "DRYICE", "STOR", "DLV", "CUSTOMS", "ACCT", "SPROG", "PPROG", "PEAK", "COST",
         "MARGIN", "HDL", "DUTY", "TAX", "LATE")

# Labels we can recognise before any contract is read (a contract's Annex E adds its own).
DEFAULT_ALIASES = {
    "FRT": ["Air freight", "Express freight", "Freight charge", "Freight"],
    "FUEL": ["Fuel surcharge", "FSC", "Fuel adj.", "Fuel"],
    "SEC": ["Security surcharge", "Screening fee", "Screening fee (adj.)", "Security screening"],
    "DG": ["Dangerous goods fee", "DG handling", "Hazmat handling", "DG fee"],
    "DRYICE": ["Dry ice handling fee", "Dry ice handling"],
    "STOR": ["Storage", "Warehouse storage", "Demurrage"],
    "DLV": ["Delivery fee", "Door delivery", "Final-mile delivery"],
    "CUSTOMS": ["Customs hold fee", "Customs examination", "Hold handling"],
    "ACCT": ["Monthly account fee", "Account maintenance"],
    "SPROG": ["Storage programme fee", "Storage programme"],
    "PPROG": ["Pickup programme fee", "Pickup programme"],
    "PEAK": ["Peak season surcharge", "PSS"],
    "COST": ["Carrier cost", "Linehaul cost pass-through"],
    "MARGIN": ["Handling margin", "Forwarding margin"],
    "HDL": ["Special handling", "Supplementary handling"],
    "DUTY": ["Import duty (disbursement)", "Duty paid on your behalf", "Import duty"],
    "TAX": ["Sales tax", "GST"],
    "LATE": ["Late payment fee", "Late fee", "Overdue interest"],
}

# What a code is for the qualifying-spend definition (clause 1.1 style).
QS_DEFAULT = {"FRT", "FUEL", "SEC", "DG", "DRYICE", "STOR", "DLV", "CUSTOMS", "ACCT", "SPROG", "PPROG", "PEAK", "COST",
              "MARGIN", "HDL"}
NEVER_QS = {"DUTY", "TAX", "LATE"}
