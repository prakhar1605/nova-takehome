# Design

The harness splits reading from judging. A model reads each document once, at ingest, into typed facts tied to the
lines they came from. A deterministic engine decides everything at report time from those facts and the glossary's
rules. Nothing about money, authority or actions is left to the model.

## Fact model

`ingest` turns every email body and attachment into a document, using the grader's own text layer (scans go through
local OCR, and the model also sees the page image). Each document is read into typed facts: contract, amendment,
annex, rate notice, waiver, due-date extension, shipment event, customs hold, invoice lines, credit note, AWB, DG
declaration, tracking, ledger, invoice cover, injection (`harness/nova/facts.py`). Each value carries
`src = [page, line(s)]`, and a quote copies those lines, so every citation resolves. Long tables are mapped, not transcribed: the model names
the columns and how raw values map to canonical events or statuses, and code parses every row.

Two readers run on every document. The LLM reader (Claude, tool calls with JSON schemas) reads by meaning. The rules
reader recognises charge lines by the contract's own Annex E labels and separates qty, rate and amount by
arithmetic, not by column position. A number the model reports must appear on the line it cites, or it is dropped
and the rules reading of that line is used. Facts are stored per document with the reader that produced them and
the document's received date. `report --as-of D` rebuilds the world from documents received by end of D only.

## Conflicts

A shipment is priced at its pickup date (tracking, then a carrier pickup email, then the AWB, then the invoice). The
rate comes from the highest instrument in force: a signed amendment or an annex it brings in (latest effective date
wins), then a rate notice whose sender is the carrier's Schedule 1 `rates` person (matched by address), then the MSA
period. Authority is checked on every email and is never taken from an email body. When an amendment and an
authorised notice disagree for the same lane and date, and the contract says they are unranked, the key is a
conflict with both sources and `[billed − higher correct, billed − lower correct]`. An unauthorised sender, quoted
text or an unsigned draft simply loses. An amendment that names an annex we don't hold makes the key
`cannot_determine`, with the name as cited and the holder from Schedule 1.

## Confidence

Each item starts from a per-class base, then loses points for weaker evidence: the two readers disagree on a
deciding line (−0.15), the pickup date came from the AWB or invoice rather than tracking (−0.08), the invoice was a
scan (−0.05), the amount is under a dollar (−0.15). Conflicts and missing-document items sit lower. On practice,
exam and my evals every item is right, so AUROC can't be measured and the Brier score is 0.006; the deductions are
ranked by how often each weakness broke a reading during development, not fitted to data.

## People in the loop

Internal actions (expiry alerts, payment reminders, credit tracking) run in both modes. Conflicts always escalate.
Outbound remedies (dispute, credit claim, document request) are drafts in supervised mode, and `human_review` once
the outbound total on one invoice or cashback key reaches $5,000. Autonomous mode executes only when all five
unattended checks pass: no missing document, pickup date from tracking or a carrier email, no conflict on the key or
the same contract and lane, under $5,000, authority at level 1–2 for terms, and a Schedule 1 recipient. A failed
check gives `human_review` with the reasons in `held_back`. Recipients only ever come from Schedule 1. Text aimed at
an AI is recorded as an `injection` fact and never acted on.

## Feedback

Each feedback item is stored as given, with its scope. Decisions on a key (close, reject, override, one-off waiver
approval) touch that key only. Standing facts (sender authority, accepted rate notice, precedence) apply to a later
key only when every `applies_to` filter matches and the shipment date is inside `valid_from`–`valid_to`. A later
signed amendment still wins. Every item whose answer changed because of feedback carries `learned_at`. Two eval
cases check that feedback does not spread: the March waiver approval must not waive April, and authority confirmed
for March must not reach an April email.

## New documents

Every report recomputes from all facts held, so a new amendment, a resend or a ledger update changes answers in
place. Report history is kept in the state folder: a changed key gets a new id and `supersedes` the old one, a key
that exists only because of a backdated document is `retro`, and keys whose window has closed disappear. A new
layout is handled by the LLM reader. The rules reader is the fallback when the API fails, and its job is to
cross-check numbers, not to know every layout.

## Cost

Measured on the exam mailbox with the cache off: 86 documents, one claude-sonnet-5-5 call each, 79k uncached and
1.04M cached input tokens (prompt and tool schemas), 57k output: $0.93, so about **$11 per 1,000 documents**. Calls
take 4 s on average and 33 s at most (a contract); with 8 workers a batch ingests in under two minutes. Reports
make no model calls.

## What I'd do next

- Calibrate confidence on a set that has wrong items: perturb the eval mailboxes (OCR noise, swapped columns) and
  fit the deductions to those errors.
- Chunk very long contracts by page instead of one call.
- Evaluate a waiver condition in general, not only "paid by date".
- Test margin step-down, which is built from the glossary but has no example in the data.
- What I am least sure about: the hidden set's feedback kinds, and how its graders name missing originals.
