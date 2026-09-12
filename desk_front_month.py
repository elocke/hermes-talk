"""Front-month resolution for futures roots — pure stdlib, offline, no network.

A vendored, deliberately small mirror of the ``nt8-contract-month`` skill's
``scripts/front_month.py`` (elocke/skills). The desk tools resolve the
tradable contract BEFORE asking NinjaTrader for a quote, because NT8 names
contracts ``ROOT MM-YY`` (``NQ 12-26``) and a quote on the wrong month comes
back as zeros, not as an error.

Rules implemented (sources in the skill's ``references/cme-roll-rules.md``):

- Equity index (ES MES NQ MNQ RTY M2K YM MYM): quarterly H M U Z; expiry is
  the 3rd Friday of the contract month; the front month rolls on the
  Thursday BEFORE that Friday (the expiring month stays front ON roll day).
- COMEX metals (GC MGC even months; SI SIL Mar/May/Jul/Sep/Dec): roll on
  the last business day of the month before delivery (~first notice day).
- NYMEX crude (CL MCL, every month): roll 3 business days before the 25th
  of the month preceding delivery.

Business days are Mon-Fri only — there is no exchange holiday calendar in
the standard library, so a date within a few days of a US market holiday
can be off by one. The authoritative expiry is always
``nt8.instrument.details``; this module only decides which contract to ask
about first.
"""

from __future__ import annotations

import calendar
import datetime as dt

MONTH_CODES = "FGHJKMNQUVXZ"  # Jan..Dec

_QUARTERLY = (3, 6, 9, 12)
_GC_MONTHS = (2, 4, 6, 8, 10, 12)
_SI_MONTHS = (3, 5, 7, 9, 12)
_ALL_MONTHS = tuple(range(1, 13))

#: root -> (family, active delivery months). Micros share the parent's cycle.
ROOTS: dict[str, tuple[str, tuple[int, ...]]] = {
    "ES": ("equity_index", _QUARTERLY),
    "MES": ("equity_index", _QUARTERLY),
    "NQ": ("equity_index", _QUARTERLY),
    "MNQ": ("equity_index", _QUARTERLY),
    "RTY": ("equity_index", _QUARTERLY),
    "M2K": ("equity_index", _QUARTERLY),
    "YM": ("equity_index", _QUARTERLY),
    "MYM": ("equity_index", _QUARTERLY),
    "GC": ("metal", _GC_MONTHS),
    "MGC": ("metal", _GC_MONTHS),
    "SI": ("metal", _SI_MONTHS),
    "SIL": ("metal", _SI_MONTHS),
    "CL": ("energy", _ALL_MONTHS),
    "MCL": ("energy", _ALL_MONTHS),
}

#: Spoken or typed alias -> NT8 root. Lower-cased lookup.
ALIASES: dict[str, str] = {
    "nq": "NQ", "nasdaq": "NQ", "nasdaq100": "NQ", "ndx": "NQ", "nas": "NQ",
    "mnq": "MNQ", "micronasdaq": "MNQ",
    "es": "ES", "spx": "ES", "sp500": "ES", "spy": "ES", "spoos": "ES",
    "mes": "MES", "microes": "MES",
    "rty": "RTY", "russell": "RTY", "russell2000": "RTY", "m2k": "M2K",
    "ym": "YM", "dow": "YM", "dowjones": "YM", "mym": "MYM",
    "gc": "GC", "gold": "GC", "mgc": "MGC", "microgold": "MGC",
    "si": "SI", "silver": "SI", "sil": "SIL", "microsilver": "SIL",
    "cl": "CL", "oil": "CL", "crude": "CL", "wti": "CL",
    "mcl": "MCL", "microcrude": "MCL",
}  # fmt: skip


def normalise_root(token: str) -> str | None:
    """Alias, ticker or continuous form (``NQ1!``) -> NT8 root, else ``None``."""

    t = token.strip().replace(" ", "").replace("-", "").replace("_", "")
    if t.upper() in ROOTS:
        return t.upper()
    key = t.lower()
    if key in ALIASES:
        return ALIASES[key]
    for suffix in ("1!", "2!", "3!", "#"):
        if key.endswith(suffix):
            base = key[: -len(suffix)]
            if base.upper() in ROOTS:
                return base.upper()
            if base in ALIASES:
                return ALIASES[base]
    return None


def third_friday(year: int, month: int) -> dt.date:
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(4 - first.weekday()) % 7 + 14)


def _is_business_day(d: dt.date) -> bool:
    return d.weekday() < 5


def _prev_business_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while not _is_business_day(d):
        d -= dt.timedelta(days=1)
    return d


def _minus_business_days(d: dt.date, n: int) -> dt.date:
    for _ in range(n):
        d = _prev_business_day(d)
    return d


def _last_business_day_of_month(year: int, month: int) -> dt.date:
    d = dt.date(year, month, calendar.monthrange(year, month)[1])
    while not _is_business_day(d):
        d -= dt.timedelta(days=1)
    return d


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def contract_dates(family: str, year: int, month: int) -> tuple[dt.date, dt.date]:
    """``(expiry, roll)`` for one contract month of one family."""

    if family == "equity_index":
        tf = third_friday(year, month)
        return tf, tf - dt.timedelta(days=1)
    if family == "metal":
        expiry = _minus_business_days(_last_business_day_of_month(year, month), 2)
        py, pm = _prev_month(year, month)
        return expiry, _last_business_day_of_month(py, pm)
    if family == "energy":
        py, pm = _prev_month(year, month)
        ref = dt.date(py, pm, 25)
        while not _is_business_day(ref):
            ref = _prev_business_day(ref)
        expiry = _minus_business_days(ref, 3)
        return expiry, expiry
    raise ValueError(f"unknown family {family!r}")


def nt8_name(root: str, year: int, month: int) -> str:
    """NT8 display form: ``ROOT MM-YY`` (``NQ 12-26``)."""

    return f"{root} {month:02d}-{year % 100:02d}"


def _candidates(root: str, asof: dt.date, count: int) -> list[dict]:
    family, months = ROOTS[root]
    out: list[dict] = []
    year = asof.year
    while len(out) < count and year <= asof.year + 3:
        for month in months:
            if dt.date(year, month, 1) < dt.date(asof.year, asof.month, 1):
                continue
            expiry, roll = contract_dates(family, year, month)
            if roll < asof and expiry < asof:
                continue
            out.append(
                {
                    "name": nt8_name(root, year, month),
                    "code": f"{root}{MONTH_CODES[month - 1]}{year % 10}",
                    "expiry": expiry,
                    "roll": roll,
                }
            )
            if len(out) >= count:
                break
        year += 1
    return out


def resolve(root: str, asof: dt.date | None = None) -> dict:
    """Front and next contract for ``root`` as of ``asof`` (default: today, UTC).

    Returns ``{root, asof, front, front_code, next, expiry, roll, roll_week}``.
    ``roll_week`` is True when the front month expires or rolls within 7
    days — the window where the higher-volume month should win and a zero
    quote on ``front`` should be retried on ``next``.
    """

    if root not in ROOTS:
        raise KeyError(root)
    asof = asof or dt.datetime.now(dt.UTC).date()
    cands = _candidates(root, asof, count=3)
    front = next((c for c in cands if c["roll"] >= asof), cands[-1])
    idx = cands.index(front)
    nxt = cands[idx + 1] if idx + 1 < len(cands) else None
    days_to_expiry = (front["expiry"] - asof).days
    days_to_roll = (front["roll"] - asof).days
    return {
        "root": root,
        "asof": asof.isoformat(),
        "front": front["name"],
        "front_code": front["code"],
        "next": nxt["name"] if nxt else None,
        "expiry": front["expiry"].isoformat(),
        "roll": front["roll"].isoformat(),
        "roll_week": days_to_expiry <= 7 or days_to_roll <= 7,
    }


__all__ = [
    "ALIASES",
    "MONTH_CODES",
    "ROOTS",
    "contract_dates",
    "normalise_root",
    "nt8_name",
    "resolve",
    "third_friday",
]
