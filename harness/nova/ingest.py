"""ingest: read one batch folder into the state folder.

1. Every .eml becomes documents (the email, each attachment; scans are OCR'd) saved under <state>/docs.
2. Each document is read into typed facts (nova/facts.py) by the LLM reader when an API key is set, with the rules
   reader as the fallback for any document the model can't read. Contracts are read first, because their Annex E
   charge labels help read the invoices.
3. The facts are saved under <state>/facts with the reader that produced them.
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import rules_reader as R
from .store import Store, load_batch


def log(msg: str) -> None:
    print(f"[ingest] {msg}", file=sys.stderr, flush=True)


def ingest(batch_dir: str, state_dir: str) -> None:
    store = Store(state_dir)
    docs = load_batch(batch_dir)
    new = [d for d in docs if not store.has_doc(d["id"])]
    for d in new:
        store.put_doc(d)
    log(f"{len(new)} new document(s) from {batch_dir}")

    use_llm = bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("NOVA_READER", "llm") != "rules"
    llm = None
    if use_llm:
        try:
            from .llm_reader import LLMReader
            llm = LLMReader(store)
        except Exception as e:                                         # noqa: BLE001
            log(f"LLM reader unavailable ({e}); using the rules reader")
            llm = None

    contracts_new = [d for d in new if R.classify(d) == "contract"]
    rest = [d for d in new if d not in contracts_new]

    def read_one(d, labels):
        facts, reader = None, "rules"
        if llm is not None:
            try:
                facts = llm.read(d, labels)
                reader = "llm"
            except Exception as e:                                     # noqa: BLE001 - fall back, never stop
                log(f"LLM read failed for {d['id']}: {type(e).__name__}: {str(e)[:200]}")
                facts = None
        rules = R.read(d, labels)
        if facts is None:
            facts = rules
        store.put_facts(d["id"], {"doc": d["id"], "reader": reader, "facts": facts,
                                  "rules_facts": rules if reader == "llm" else None})

    workers = int(os.environ.get("NOVA_WORKERS", "8"))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda d: read_one(d, {}), contracts_new))
    # the label index from every contract we hold (earlier batches included)
    contract_facts = []
    for d in store.docs():
        fx = store.facts(d["id"]) or {}
        contract_facts += [f for f in fx.get("facts") or [] if f.get("type") == "contract"]
    labels = R.build_label_index(contract_facts)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda d: read_one(d, labels), rest))
    log("done")
