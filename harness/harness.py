"""Nova freight-invoice audit harness. The grader calls exactly these three commands, each in a fresh process:

    python harness.py ingest <batch_dir> --state-dir <dir>
    python harness.py report --as-of YYYY-MM-DD [--mode supervised|autonomous] [--state-dir <dir>] > report.json
    python harness.py feedback <feedback.json> --state-dir <dir>

How it works (details in DESIGN.md and the docstrings under harness/nova/):
- ingest   reads every email and attachment (OCR for scans), and an LLM reader (rules reader as fallback) turns each
           document into typed facts with the exact source line, saved under --state-dir.
- report   a deterministic rules engine rebuilds the world as of the date (only documents received by then), runs
           the checks (overcharge, duplicate, waived but billed, late fees, cashback, expiry, conflicts, missing
           documents), applies feedback, chooses the action for the mode and prints report.json.
- feedback stores a person's decisions with their scope; later reports apply them only where the scope matches.

The environment gives ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_STATE = ".state"


def ingest(batch_dir: str, state_dir: str) -> None:
    from nova.ingest import ingest as run
    run(batch_dir, state_dir)


def report(as_of: str, mode: str, state_dir: str) -> dict:
    from nova.report import build
    from nova.store import Store
    return build(Store(state_dir), as_of, mode)


def feedback(feedback_file: str, state_dir: str) -> None:
    from nova.store import Store
    fb = json.loads(Path(feedback_file).read_text())
    Store(state_dir).add_feedback(fb, Path(feedback_file).name)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py", description="Freight invoice audit harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="read one batch folder into the state dir")
    p.add_argument("batch_dir")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("report", help="print report.json for a date")
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD; only documents received by then count")
    p.add_argument("--mode", choices=["supervised", "autonomous"], default="supervised")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("feedback", help="take in a person's decisions (feedback.json)")
    p.add_argument("feedback_file")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ingest":
            ingest(a.batch_dir, a.state_dir)
        elif a.cmd == "report":
            json.dump(report(a.as_of, a.mode, a.state_dir), sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            feedback(a.feedback_file, a.state_dir)
    except NotImplementedError as e:
        print(f"harness.py {a.cmd}: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
