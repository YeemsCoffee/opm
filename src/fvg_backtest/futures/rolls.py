"""Contract roll handling for continuous research series.

A *roll schedule* assigns exactly one front contract to every session date.
Three explicit methods are supported (no silent combining):

- ``HIGHEST_VOLUME``: roll to the next contract the session after its daily
  volume first exceeds the current front's (requires per-contract volumes).
- ``FIXED_DAYS_BEFORE_EXPIRATION``: roll N calendar days before expiration.
- ``USER_DEFINED_ROLL_CALENDAR``: explicit ``{contract: last-front-date}``.

Continuous series are **not** back-adjusted by default: the strategy depends
on actual price levels (gaps, swings, stops).  ``back_adjust`` exists but is
loudly warned about wherever it is exposed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from ..config.schema import InstrumentConfig, RollConfig
from .contracts import Contract, contract_expiration, list_contracts, parse_contract


class RollPeriod(StrEnum):
    NORMAL = "NORMAL"
    ROLLOVER_TRANSITION = "ROLLOVER_TRANSITION"
    EXPIRATION_WEEK = "EXPIRATION_WEEK"


@dataclass(frozen=True)
class RollSegment:
    contract: Contract
    first_session: date  # inclusive
    last_session: date  # inclusive (last date this contract is front)


def _fixed_roll_date(c: Contract, instrument: InstrumentConfig, days: int) -> date:
    return contract_expiration(c, instrument) - timedelta(days=days)


def build_roll_schedule(
    instrument: InstrumentConfig,
    start: date,
    end: date,
    config: RollConfig,
    daily_volumes: dict[str, dict[date, float]] | None = None,
) -> list[RollSegment]:
    """Return ordered roll segments covering [start, end].

    ``daily_volumes`` maps contract code -> {session_date: volume} and is
    required for HIGHEST_VOLUME (built upstream from per-contract candles).
    """
    contracts = list_contracts(instrument, start, end, include_prior=1)
    if not contracts:
        raise ValueError("no contracts found for range")

    # last date each contract remains front (its "roll date")
    last_front: dict[str, date] = {}
    for c in contracts:
        expiry = contract_expiration(c, instrument)
        if config.method == "FIXED_DAYS_BEFORE_EXPIRATION":
            last_front[c.code] = _fixed_roll_date(c, instrument, config.fixed_days_before_expiration)
        elif config.method == "USER_DEFINED_ROLL_CALENDAR":
            if c.code in config.roll_calendar:
                last_front[c.code] = date.fromisoformat(config.roll_calendar[c.code])
            else:
                # calendar gaps fall back to expiration eve
                last_front[c.code] = expiry - timedelta(days=1)
        elif config.method == "HIGHEST_VOLUME":
            last_front[c.code] = _volume_roll_date(c, contracts, instrument, daily_volumes)
        else:  # pragma: no cover
            raise ValueError(f"unknown roll method {config.method}")
        # never stay front past expiration eve
        last_front[c.code] = min(last_front[c.code], expiry - timedelta(days=1))

    segments: list[RollSegment] = []
    cursor = start
    for c in contracts:
        lf = last_front[c.code]
        if lf < cursor:
            continue
        seg_end = min(lf, end)
        segments.append(RollSegment(contract=c, first_session=cursor, last_session=seg_end))
        cursor = seg_end + timedelta(days=1)
        if cursor > end:
            break
    if cursor <= end and segments:
        # extend the final contract if the range outlives the schedule
        last = segments[-1]
        segments[-1] = RollSegment(last.contract, last.first_session, end)
    return segments


def _volume_roll_date(
    c: Contract,
    contracts: list[Contract],
    instrument: InstrumentConfig,
    daily_volumes: dict[str, dict[date, float]] | None,
) -> date:
    """First day the *next* quarterly contract out-trades ``c`` ends c's reign.

    Without volume data we fall back to 8 calendar days before expiration
    (the typical equity-index roll week).
    """
    expiry = contract_expiration(c, instrument)
    fallback = expiry - timedelta(days=8)
    idx = contracts.index(c)
    nxt = contracts[idx + 1] if idx + 1 < len(contracts) else None
    if nxt is None or not daily_volumes:
        return fallback
    cur_vol = daily_volumes.get(c.code, {})
    nxt_vol = daily_volumes.get(nxt.code, {})
    days = sorted(set(cur_vol) & set(nxt_vol))
    for d in days:
        if d >= expiry:
            break
        if nxt_vol[d] > cur_vol[d]:
            return d  # last day as front = the day it was first out-traded
    return fallback


def front_contract_for(schedule: list[RollSegment], session: date) -> Contract | None:
    for seg in schedule:
        if seg.first_session <= session <= seg.last_session:
            return seg.contract
    return None


def classify_roll_period(
    session: date,
    contract: Contract | str,
    instrument: InstrumentConfig,
    config: RollConfig,
) -> tuple[RollPeriod, int]:
    """Classify a session for rollover comparisons; returns (period, DTE).

    EXPIRATION_WEEK: Monday–Friday of the contract's expiration week.
    ROLLOVER_TRANSITION: within ``rollover_window_days`` calendar days before
    the contract's roll date (or expiration when the roll date is later).
    """
    c = parse_contract(contract) if isinstance(contract, str) else contract
    expiry = contract_expiration(c, instrument)
    dte = (expiry - session).days
    week_monday = expiry - timedelta(days=expiry.weekday())
    if week_monday <= session <= week_monday + timedelta(days=4):
        return RollPeriod.EXPIRATION_WEEK, dte
    if config.method == "FIXED_DAYS_BEFORE_EXPIRATION":
        roll_date = expiry - timedelta(days=config.fixed_days_before_expiration)
    elif config.method == "USER_DEFINED_ROLL_CALENDAR" and c.code in config.roll_calendar:
        roll_date = date.fromisoformat(config.roll_calendar[c.code])
    else:
        roll_date = expiry - timedelta(days=8)
    if roll_date - timedelta(days=config.rollover_window_days) <= session <= max(roll_date, expiry):
        return RollPeriod.ROLLOVER_TRANSITION, dte
    return RollPeriod.NORMAL, dte
