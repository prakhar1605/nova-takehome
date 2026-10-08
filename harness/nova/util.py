"""Small shared helpers: money, dates, ids, lanes. Everything money is Decimal, rounded half up to cents."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0")


def D(x) -> Decimal | None:
    """Decimal from a number or a printed amount ('1,701.31', 'USD 52.70', 'INR127,024.44', '18.38%')."""
    if x is None:
        return None
    if isinstance(x, Decimal):
        return x
    if isinstance(x, (int, float)):
        return Decimal(str(x))
    s = str(x).strip().replace(",", "").replace("%", "")
    s = re.sub(r"^[A-Za-z$₹€£\s]+", "", s)
    s = re.sub(r"[A-Za-z\s]+$", "", s)
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def r2(x: Decimal) -> Decimal:
    """Round half up (ties away from zero) to cents."""
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def money(x) -> str:
    return f"{r2(D(x) or ZERO):.2f}"


def pct_of(pct: Decimal, base: Decimal) -> Decimal:
    return r2(Decimal(pct) / Decimal(100) * Decimal(base))


# ---- dates ---------------------------------------------------------------------------------------------------------
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})

ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
DMY_RE = re.compile(r"\b(\d{1,2})[ -]([A-Za-z]{3,9})\.?[ -](\d{4})\b")
MDY_RE = re.compile(r"\b([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})\b")


def parse_date(s, dayfirst: bool = True) -> date | None:
    """ISO, DD/MM/YYYY (or MM/DD with dayfirst=False), '05 Jun 2035', 'June 5, 2035'."""
    if s is None:
        return None
    if isinstance(s, date):
        return s
    s = str(s).strip()
    m = ISO_RE.search(s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = SLASH_RE.search(s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        d_, mo = (a, b) if dayfirst else (b, a)
        if mo > 12 and d_ <= 12:
            d_, mo = mo, d_
        try:
            return date(y, mo, d_)
        except ValueError:
            return None
    m = DMY_RE.search(s)
    if m and m[2].lower() in MONTHS:
        try:
            return date(int(m[3]), MONTHS[m[2].lower()], int(m[1]))
        except ValueError:
            return None
    m = MDY_RE.search(s)
    if m and m[1].lower() in MONTHS:
        try:
            return date(int(m[3]), MONTHS[m[1].lower()], int(m[2]))
        except ValueError:
            return None
    return None


def iso(d) -> str | None:
    d = parse_date(d) if not isinstance(d, date) else d
    return d.isoformat() if d else None


def ym(d) -> str:
    d = parse_date(d)
    return f"{d.year:04d}-{d.month:02d}"


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:                     # 29 Feb
        return d.replace(year=d.year + n, day=28)


def quarter_of(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def quarter_bounds(q: str) -> tuple[date, date]:
    y, n = int(q[:4]), int(q[-1])
    start = date(y, 3 * (n - 1) + 1, 1)
    end_month = 3 * n
    end = date(y, end_month, calendar.monthrange(y, end_month)[1])
    return start, end


def days(n: int) -> timedelta:
    return timedelta(days=n)


# ---- ids -----------------------------------------------------------------------------------------------------------
def norm_invoice(s) -> str:
    """As the glossary says: as printed, upper case, without spaces, '#' or ':'."""
    return re.sub(r"[\s#:]", "", str(s or "")).upper()


def norm_label(s) -> str:
    """Charge labels compared without case, spaces or punctuation ('Fuel adj.' == 'fueladj')."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


LANE_RE = re.compile(r"\b([A-Z]{3,4}\d{1,2}-[A-Z]{3,4}\d{1,2})\b")
AWB_RE = re.compile(r"\b([A-Z]{2,3}\d{7,10}(?:-H\d+)?)\b")


def norm_person(s) -> str:
    return " ".join(str(s or "").lower().split())
