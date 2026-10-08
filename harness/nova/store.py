"""The state folder. Everything the harness learns lives here, so a fresh process can pick up where the last one left
off (the grader restarts the harness for every step).

    <state>/docs/<key>.json        one per email body or attachment: text pages, email metadata, received date
    <state>/facts/<key>.json       what the extractor read from that document (typed facts, each with its source line)
    <state>/feedback/<n>.json      every feedback file, as delivered, with the order it arrived in
    <state>/history.json           the items of every report printed so far (for supersedes / retro)
    <state>/llm_cache/<sha>.json   model answers keyed by prompt hash, so a re-run never pays twice
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import textlayer as T


def _key(doc_id: str) -> str:
    return hashlib.sha1(doc_id.encode()).hexdigest()[:16]


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        for d in ("docs", "facts", "feedback", "llm_cache"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    # ---- documents -------------------------------------------------------------------------------------------------
    def put_doc(self, doc: dict) -> None:
        self._write(self.root / "docs" / f"{_key(doc['id'])}.json", doc)

    def docs(self) -> list[dict]:
        out = [json.loads(p.read_text()) for p in (self.root / "docs").glob("*.json")]
        return sorted(out, key=lambda d: (d["received_at"], d["id"]))

    def has_doc(self, doc_id: str) -> bool:
        return (self.root / "docs" / f"{_key(doc_id)}.json").exists()

    # ---- facts -----------------------------------------------------------------------------------------------------
    def put_facts(self, doc_id: str, facts: dict) -> None:
        self._write(self.root / "facts" / f"{_key(doc_id)}.json", facts)

    def facts(self, doc_id: str) -> dict | None:
        p = self.root / "facts" / f"{_key(doc_id)}.json"
        return json.loads(p.read_text()) if p.exists() else None

    # ---- feedback --------------------------------------------------------------------------------------------------
    def add_feedback(self, fb: dict, name: str) -> None:
        n = len(list((self.root / "feedback").glob("*.json")))
        self._write(self.root / "feedback" / f"{n:04d}_{Path(name).stem}.json", fb)

    def feedback(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted((self.root / "feedback").glob("*.json"))]

    # ---- report history --------------------------------------------------------------------------------------------
    def history(self) -> list[dict]:
        p = self.root / "history.json"
        return json.loads(p.read_text()) if p.exists() else []

    def save_history(self, h: list[dict]) -> None:
        self._write(self.root / "history.json", h)

    # ---- misc ------------------------------------------------------------------------------------------------------
    def cache_path(self, key: str) -> Path:
        return self.root / "llm_cache" / f"{key}.json"

    @staticmethod
    def _write(path: Path, obj) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))
        os.replace(tmp, path)


def load_batch(batch_dir: str | Path) -> list[dict]:
    """Read every .eml in a batch folder into documents: the email itself (one page) and each attachment (its
    pages). Scans get OCR text and are marked `scan`. Document ids are what evidence cites:
    'batch_01/0004.eml' and 'batch_01/0004.eml#INV-1.pdf'."""
    bdir = Path(batch_dir)
    batch = bdir.name
    out = []
    for f in sorted(bdir.glob("*.eml")):
        raw = f.read_bytes()
        msg = T.parse_eml(raw)
        meta = T.email_meta(msg)
        eid = f"{batch}/{f.name}"
        base = {"email": eid, "batch": batch, "received": meta["received"], "received_at": meta["date"],
                "meta": meta}
        out.append({**base, "id": eid, "name": f.name, "ext": "eml", "scan": False,
                    "pages": ["\n".join(T.email_lines(msg))]})
        for name, data in T.attachments(raw):
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            doc = {**base, "id": f"{eid}#{name}", "name": name, "ext": ext, "scan": False, "pages": []}
            try:
                if ext == "pdf":
                    if T.pdf_is_image_only(data):
                        doc["scan"] = True
                        try:
                            doc["pages"] = T.ocr_pdf(data)
                        except Exception as e:          # noqa: BLE001 - OCR missing: the vision model may still read it
                            doc["pages"] = [""]
                            doc["ocr_error"] = str(e)[:200]
                    else:
                        doc["pages"] = T.pdf_pages(data)
                elif ext in ("xlsx", "xlsm"):
                    doc["pages"] = T.xlsx_pages(data)
                elif ext in ("txt", "csv", "md"):
                    doc["pages"] = [data.decode("utf-8", "replace")]
                else:
                    doc["pages"] = [""]
            except Exception as e:                     # noqa: BLE001 - an unreadable file is kept, flagged, and skipped
                doc["error"] = str(e)[:200]
                doc["pages"] = doc["pages"] or [""]
            if doc["scan"]:
                doc["pdf_b64"] = __import__("base64").b64encode(data).decode()
            out.append(doc)
    return out
