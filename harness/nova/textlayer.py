"""The text layer: every email and attachment read back as pages of lines, exactly the way the grader reads them.

This is a copy of the reading rules in grader/extract.py (the harness runs in its own folder, so it does not import
the grader). Using the same reader means every quote we cite is a whole line of a page the grader can find.

Scanned PDFs (no text layer) are read with local OCR (rapidocr) and, when an API key is present, can be re-read
with a vision model by the extractor. The grader cannot check quotes on a scan, so OCR text is fine for quoting.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime, getaddresses

SEP = " | "


def norm(s: str) -> str:
    return " ".join(s.split())


# ---- PDF -----------------------------------------------------------------------------------------------------------
def _reading_order(page) -> str:
    words = page.get_text("words")
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[list] = []
    for w in words:
        mid = (w[1] + w[3]) / 2
        if lines and abs(lines[-1][0] - mid) <= 2.5:
            lines[-1][1].append(w)
        else:
            lines.append([mid, [w]])
    return "\n".join(" ".join(x[4] for x in sorted(ws, key=lambda w: w[0])) for _, ws in lines)


def pdf_pages(data: bytes) -> list[str]:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [_reading_order(p) for p in doc]


def pdf_is_image_only(data: bytes) -> bool:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return all(not p.get_text().strip() for p in doc)


def pdf_page_png(data: bytes, page_no: int, dpi: int = 200) -> bytes:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return doc[page_no].get_pixmap(dpi=dpi).tobytes("png")


_OCR = None


def ocr_pdf(data: bytes) -> list[str]:
    """Local OCR of an image-only PDF: rapidocr boxes grouped into lines by their vertical position, left to right.
    rapidocr drops most spaces inside a box ("Airfreight", "106kg"); the extractor compares labels with spaces
    removed, so that is fine."""
    global _OCR
    import pymupdf
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    pages = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for p in doc:
            png = p.get_pixmap(dpi=200).tobytes("png")
            res, _ = _OCR(png)
            rows: list[list] = []
            for box, txt, _conf in sorted(res or [], key=lambda r: ((r[0][0][1] + r[0][2][1]) / 2, r[0][0][0])):
                y = (box[0][1] + box[2][1]) / 2
                h = abs(box[2][1] - box[0][1]) or 20
                if rows and abs(rows[-1][0] - y) < max(8.0, h * 0.45):
                    rows[-1][1].append((box[0][0], txt))
                else:
                    rows.append([y, [(box[0][0], txt)]])
            pages.append("\n".join(" ".join(t for _, t in sorted(ws)) for _, ws in rows))
    return pages


# ---- XLSX ----------------------------------------------------------------------------------------------------------
def _cell_text(cell) -> str:
    v = cell.value
    if v is None:
        return ""
    fmt = cell.number_format or "General"
    prefix = ""
    m = re.match(r'^"([^"]*)"(.*)$', fmt)
    if m:
        prefix, fmt = m.group(1), m.group(2)
    if isinstance(v, datetime):
        return prefix + v.date().isoformat()
    if isinstance(v, date):
        return prefix + v.isoformat()
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        dp = len(fmt.split(".")[1]) if "." in fmt else 0
        return prefix + f"{Decimal(str(v)):.{dp}f}"
    return prefix + str(v)


def xlsx_pages(data: bytes) -> list[str]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    pages = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows():
            cells = [t for t in (_cell_text(c) for c in row) if t != ""]
            if cells:
                rows.append(SEP.join(cells))
        pages.append("\n".join(rows))
    return pages


# ---- email ---------------------------------------------------------------------------------------------------------
def parse_eml(data: bytes):
    return BytesParser(policy=policy.default).parsebytes(data)


def email_lines(msg) -> list[str]:
    name, addr = parseaddr(str(msg["From"]))
    out = [f"From: {name} <{addr}>", f"To: {msg['To']}"]
    if msg["Cc"]:
        out.append(f"Cc: {msg['Cc']}")
    dt = parsedate_to_datetime(str(msg["Date"]))
    out += [f"Date: {dt.strftime('%Y-%m-%dT%H:%M:%SZ')}", f"Subject: {msg['Subject']}", ""]
    body = msg.get_body(preferencelist=("plain",))
    out += body.get_content().replace("\r\n", "\n").rstrip("\n").split("\n") if body else []
    names = [p.get_filename() for p in msg.iter_attachments()]
    if names:
        out.append("Attachments: " + ", ".join(names))
    return out


def email_meta(msg) -> dict:
    name, addr = parseaddr(str(msg["From"]))
    dt = parsedate_to_datetime(str(msg["Date"]))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return {
        "from_name": name, "from_email": addr.lower(),
        "to": [a.lower() for _, a in getaddresses([str(msg["To"] or "")]) if a],
        "cc": [a.lower() for _, a in getaddresses([str(msg["Cc"] or "")]) if a],
        "date": dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "received": dt.date().isoformat(),
        "subject": str(msg["Subject"] or ""), "message_id": str(msg["Message-ID"] or ""),
        "in_reply_to": str(msg["In-Reply-To"] or ""),
        "attachments": [p.get_filename() or "" for p in msg.iter_attachments()],
    }


def attachments(data: bytes) -> list[tuple[str, bytes]]:
    return [(p.get_filename() or "", p.get_content()) for p in parse_eml(data).iter_attachments()]
