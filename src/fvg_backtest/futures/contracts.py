"""Dated futures contract handling for NQ / MNQ.

Canonical contract codes look like ``NQH25`` (root + month letter + 2-digit
year).  Databento GLBX raw symbols use a single-digit year (``NQH5``); both
forms are parsed, and :func:`to_databento_raw` maps canonical -> provider.

Expiration follows the instrument's ``expiration_rule`` (CME equity index:
9:30 a.m. New York on the third Friday of the contract month).  These are
metadata-driven — nothing here is NQ-specific beyond configuration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from ..config.schema import InstrumentConfig

MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}
CODE_FOR_MONTH = {v: k for k, v in MONTH_CODES.items()}

_CONTRACT_RE = re.compile(r"^([A-Z]{1,3}?)([FGHJKMNQUVXZ])(\d{1,2})$")


@dataclass(frozen=True)
class Contract:
    root: str  # NQ | MNQ
    month: int  # 1..12
    year: int  # 4-digit

    @property
    def code(self) -> str:
        return f"{self.root}{CODE_FOR_MONTH[self.month]}{self.year % 100:02d}"

    @property
    def databento_raw(self) -> str:
        return f"{self.root}{CODE_FOR_MONTH[self.month]}{self.year % 10}"

    def __str__(self) -> str:  # pragma: no cover
        return self.code


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    # weekday(): Monday=0 ... Friday=4
    first_friday = d + timedelta(days=(4 - d.weekday()) % 7)
    return first_friday + timedelta(days=14)


def contract_expiration(contract: Contract, instrument: InstrumentConfig) -> date:
    """Expiration *date* of a contract per the instrument's rule."""
    if instrument.expiration_rule == "THIRD_FRIDAY_930":
        return _third_friday(contract.year, contract.month)
    raise ValueError(f"unknown expiration rule {instrument.expiration_rule!r}")


def parse_contract(code: str, reference: date | None = None) -> Contract:
    """Parse ``NQH25`` / ``MNQZ24`` / Databento-style ``NQH5``.

    Single-digit years are resolved to the closest decade that keeps the
    contract within [reference - 2y, reference + 8y] (reference defaults to
    today), matching how front-month symbols are used in practice.
    """
    code = code.strip().upper()
    m = _CONTRACT_RE.match(code)
    if not m:
        raise ValueError(f"unrecognized contract code: {code!r}")
    root, month_code, year_txt = m.groups()
    month = MONTH_CODES[month_code]
    if len(year_txt) == 2:
        year = 2000 + int(year_txt)
    else:
        # single digit: exactly one candidate lands in the 10-year window
        # [reference-2, reference+7]
        ref = reference or date.today()
        digit = int(year_txt)
        base = ref.year - (ref.year % 10)
        year = next(
            y
            for y in (base - 10 + digit, base + digit, base + 10 + digit)
            if ref.year - 2 <= y <= ref.year + 7
        )
    return Contract(root=root, month=month, year=year)


def format_contract(root: str, month: int, year: int) -> str:
    return Contract(root=root, month=month, year=year).code


def to_databento_raw(code: str, reference: date | None = None) -> str:
    return parse_contract(code, reference).databento_raw


def list_contracts(
    instrument: InstrumentConfig,
    start: date,
    end: date,
    include_prior: int = 1,
) -> list[Contract]:
    """All dated contracts of ``instrument`` whose expiration falls in or
    around [start, end], ordered by expiration.  ``include_prior`` extra
    contracts before ``start`` are included so roll logic has a front
    contract on the first requested day.
    """
    months = sorted(MONTH_CODES[m] for m in instrument.contract_months)
    out: list[Contract] = []
    for year in range(start.year - 1, end.year + 2):
        for month in months:
            c = Contract(root=instrument.root, month=month, year=year)
            out.append(c)
    out.sort(key=lambda c: contract_expiration(c, instrument))
    # keep contracts expiring after `start` (plus `include_prior` before it)
    # and starting before a year past `end`
    expiries = [contract_expiration(c, instrument) for c in out]
    first_live = next((i for i, e in enumerate(expiries) if e >= start), len(out))
    lo = max(0, first_live - include_prior)
    hi = next((i for i, e in enumerate(expiries) if e > end + timedelta(days=200)), len(out))
    return out[lo:hi]
