# My solution

## How to run it

```
make setup                         # .venv from harness/requirements.txt; nothing else is needed
export ANTHROPIC_API_KEY=...       # the LLM reader; ANTHROPIC_BASE_URL is honoured if set
make grade                         # practice mailbox, both modes -> report_card.txt / .json
make sheet                         # exam mailbox -> out/exam_report.json, answer_sheet.json
make eval                          # evals/cases (30 cases, 28 of mine)
python tools/compare_readers.py out/state_exam   # where the LLM and rules readers disagree
```

The harness is `harness/harness.py` plus the package `harness/nova/`. No packages beyond the pinned requirements.
The model is `claude-sonnet-5-5` (override with `NOVA_MODEL`). Ingest uses 8 parallel workers (`NOVA_WORKERS`). With
no API key, or if a call fails, the rules reader reads that document instead, so the harness never stops on the
model. `NOVA_READER=rules` forces the rules reader everywhere. Model answers are cached by prompt hash in the state
folder (and in `harness/.llm_cache/` locally, gitignored), so re-running a batch costs nothing.
`evals/build_velloran.py` and `evals/build_kestrel.py` regenerate my eval mailboxes and cases.

## What works

- Practice report card: **99.9 / 100** with the LLM reader and with the rules reader. Every component is at full
  marks except confidence, where AUROC can't be measured because no item is wrong. No hard gate.
- Exam answer sheet: 12 items. I checked every one by hand against the documents, the scanned invoice included.
  The LLM and rules readers give the same sheet. Totals: overpayment $3,847.70, cashback $4,307.34, late fees
  avoided $10.29, total $8,165.33. One conflict (CDU-3051302) and one cannot_determine (CDU-3051286, Annex A rev 2,
  held by Joaquin Rathbone).
- My eval set: **30 / 30** (`evals/results.json`), with the LLM reader and with the rules reader alone. 25 cases run on a world I built (`evals/mailboxes/velloran`):
  a new carrier, customer, people, lanes and invoice layout (DD.MM.YYYY dates, "22.10% of 726.00" fuel lines),
  renamed tracking and ledger columns, "written off" waivers, "may now be made up to … without penalty"
  extensions, overdue-interest lines, an auto-renewing contract, a prompt-injection email, a forwarded-only waiver,
  a waiver from someone without authority, a backdated amendment, a carrier-fault customs hold, a consignee
  collection, a partly credited duplicate, an invoice over $5,000, a pickup date known only from the invoice, and
  five feedback cases (override, reject, one-off waiver, standing authority, and two checks that feedback does not
  spread). Three more (`evals/mailboxes/kestrel`) cover a forwarding contract with a handling margin: the margin
  step-down owed for a quarter as one cashback item, a margin billed above the contract percentage, and a check that
  a stepped-down invoice is not also reported as an overcharge.
- All six classes, conflicts, cannot_determine, both modes with the unattended test, supersedes / retro /
  learned_at, and every feedback kind in the glossary.

## What doesn't

- Confidence is a hand-set deduction model. It ranks plausibly, but with no wrong items to learn from it isn't
  calibrated.
- A waiver condition is evaluated only when it is about payment by a date; any other condition makes the waiver not
  count yet (no finding).
- Margin step-down is tested only on my own Kestrel world, since nothing in the practice or exam data has a margin.
- Chargeable weight comes from the AWB when we hold it, else as printed. We don't recompute volumetric weight from
  the dimensions.
- One model call per document. A contract much longer than the ones here (11 pages) may need chunking.
- If the API is unavailable, unfamiliar layouts fall back to the rules reader, which will miss lines it can't
  recognise.

## The eval case that caught a bug

`vel_delivery_collected` (delivery fee on a shipment the consignee collected) failed on its first run: the key was
missing. The rules reader found tracking exports only by the practice header (`AWB | Event | Date | Station`).
My export uses `Waybill | Date | Status | Location | Note` with words like "Collected by consignee", so it read zero
events. With no `COLLECTED_BY_CONSIGNEE` event the delivery fee looked owed, and the pickup dates silently fell
back to the invoice. I rewrote table detection to find the header by meaning (any waybill column plus any
status/event column, wherever it sits) and to map event words onto the canonical events. Ledger statuses got the
same treatment ("Settled", "Unpaid" → paid / open). The same run of the eval set also caught credit-note references
being dropped (`vel_duplicate_alias_partial_credit` reported 151.37 instead of 101.37) and ISO dates with a time
part not parsing (`vel_fb_one_off_waiver`).
