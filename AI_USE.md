# AI use

## Tools

- **Claude (Opus 5.5) in the Claude app, as a coding agent** with a shell, file tools and the repo. It wrote the
  harness, the eval world and these documents, and ran the grader, `make sheet`, `make eval` and `make validate`.
- **Claude Sonnet 5.5 through the API** is the harness's own document reader at runtime (`harness/nova/llm_reader.py`).
- No other AI tools.

## How you worked with them

I gave the agent the assignment email and the repo link, asked it to explain the task to me and to build the
solution, and supplied the API key and the GitHub repo. The agent planned the split between the two layers: a
model reads documents into facts, deterministic code judges them.

What was checked, and how:
- Every exam answer was worked out by hand from the documents (rates, bands, fuel months, FX, due dates, ledger
  status) before trusting the harness's sheet.
- Both readers run on every document. `tools/compare_readers.py` lists every field where they disagree; each
  difference was looked at by hand.
- A second, independent mailbox (`evals/mailboxes/velloran`, built by `evals/build_velloran.py`) has its expected
  amounts computed in the generator from the contract numbers, not from the harness.

Where the AI got things wrong, and what caught it:
- The first rules-only build passed practice but read zero events from a tracking export with other column names,
  and dropped credit-note references. The eval set caught both (see SOLUTION.md).
- In the first run with the live model, practice dropped from 99.9 to 92.1. The model recorded an "invoice cover"
  for the invoice PDF as well as the email, which overwrote the email's own "please pay again" reading, so a
  duplicate was missed. It also wrote UN numbers as `UN1845`, which the dry-ice check didn't match, so the wrong fee
  was disputed. Both were fixed in post-processing (covers only from email bodies; UN numbers reduced to digits),
  and practice went back to 99.9.
- The first API calls failed: this model rejects `tool_choice: any`. The agent switched to `auto` with an
  instruction to answer only with tool calls, and to streaming so long contracts don't hit the non-streaming limit.

## Written without AI

The code, the eval world and the write-ups were written by the agent under my direction. My part was the task
framing, the decisions the agent asked me about (repo, key, spend), reviewing the results and the hand checks of
the exam answers with it, and the walkthrough I prepared for the call.

## Prompt log

The full session export is in `ai_log/` (trimmed only for the API key). My prompts, in order:

1. "mujhe ek project mil hai tumko us project ko achhe se samjhana or complete karwana hai assignment hai" + the
   assignment email from GoComet and https://github.com/gocomet-india/nova-takehome (and the README pasted as a file).
2. The API key (removed here) + "repo ka name tum btao".
3. Follow-ups about the repo and submission.

What the agent did between prompts, in short:
1. Cloned the repo; read README, GLOSSARY, every worked example, the grader (`score.py`, `grade_sheet.py`,
   `extract.py`), and every document in the practice and exam mailboxes; tested local OCR on both scans. Explained
   the task back to me in Hinglish and asked for a key and a repo.
2. Built the text layer (a copy of the grader's reader), the rules reader, the world model, pricing, the checks,
   feedback, and report building. Practice 99.9 with the rules reader; exam sheet hand-checked.
3. Wrote the LLM reader (tool schemas, number check against the cited line, table mapping, cache, fallback). Built
   the Velloran eval world and 25 cases, fixed the three bugs they caught, and reached 27/27. Later added the
   Kestrel world (3 handling-margin cases).
4. With the key: fixed the `tool_choice` and streaming errors, ran practice (92.1 → found and fixed the two
   misreadings → 99.9), the exam sheet (same as the rules reader) and the evals (27/27), compared the readers
   field by field, and measured cost with the cache off ($0.93 for 86 documents).
5. Wrote DESIGN.md, SOLUTION.md and this file, ran `make validate`, and pushed to the private repo.
