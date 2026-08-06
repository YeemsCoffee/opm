"""Intrabar event sequencing.

A one-minute bar that touches entry, stop and target tells us *that* they
were reached, not in which order.  Three modes resolve this:

``ONE_MINUTE_CONSERVATIVE``
    Assume the adverse sequence and log the ambiguity:

    - entry and stop in the same candle -> entry, then stop;
    - stop and target after entry -> stop first;
    - the favourable extreme is **not** credited on a bar that resolves
      adversely, because the assumed path runs straight to the stop.

``ONE_SECOND_INTRABAR`` / ``TICK_INTRABAR``
    Replay the finer series inside the minute so the true order is known.
    Ambiguity survives only when a single second (or tick) touches both the
    stop and the target, where the conservative rule applies again.

Excursion accounting always mirrors the resolved sequence, so MAE/MFE can
never describe a path the simulator did not assume.  On the entry bar the
favourable extreme is credited only up to what is provably post-entry: a
long fills on the way down, so the bar's low is certainly after the fill
while its high may precede it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SequenceAmbiguity(StrEnum):
    NONE = "NONE"
    ENTRY_AND_STOP_SAME_BAR = "ENTRY_AND_STOP_SAME_BAR"
    STOP_AND_TARGET_SAME_BAR = "STOP_AND_TARGET_SAME_BAR"
    ENTRY_STOP_TARGET_SAME_BAR = "ENTRY_STOP_TARGET_SAME_BAR"
    ENTRY_AND_TARGET_SAME_BAR = "ENTRY_AND_TARGET_SAME_BAR"


@dataclass
class BarEvents:
    """What happened inside one bar, in resolved order."""

    sequence: list[tuple[str, float, datetime]] = field(default_factory=list)
    ambiguity: SequenceAmbiguity = SequenceAmbiguity.NONE
    excursion_low: float | None = None
    excursion_high: float | None = None
    resolution: str = "ONE_MINUTE_CONSERVATIVE"

    @property
    def ambiguous(self) -> bool:
        return self.ambiguity != SequenceAmbiguity.NONE

    @property
    def kinds(self) -> list[str]:
        return [k for k, _, _ in self.sequence]

    def first(self, kind: str) -> tuple[str, float, datetime] | None:
        return next((e for e in self.sequence if e[0] == kind), None)


def touches_entry(direction: str, low: float, high: float, entry: float) -> bool:
    """Limit entry: a long is filled on the way down, a short on the way up."""
    return low <= entry if direction == "LONG" else high >= entry


def touches_stop(direction: str, low: float, high: float, stop: float) -> bool:
    return low <= stop if direction == "LONG" else high >= stop


def touches_target(direction: str, low: float, high: float, target: float) -> bool:
    return high >= target if direction == "LONG" else low <= target


@dataclass
class IntrabarResolver:
    mode: str = "ONE_MINUTE_CONSERVATIVE"

    # -- public API ---------------------------------------------------------

    def resolve(
        self,
        bar: dict,
        *,
        direction: str,
        entry: float | None,
        stop: float | None,
        target: float | None,
        position_open: bool,
        finer: list[dict] | None = None,
    ) -> BarEvents:
        if self.mode != "ONE_MINUTE_CONSERVATIVE" and finer:
            return self._resolve_finer(
                finer, direction=direction, entry=entry, stop=stop,
                target=target, position_open=position_open, minute=bar,
            )
        return self._resolve_conservative(
            bar, direction=direction, entry=entry, stop=stop,
            target=target, position_open=position_open,
        )

    # -- conservative -------------------------------------------------------

    def _resolve_conservative(
        self, bar, *, direction, entry, stop, target, position_open
    ) -> BarEvents:
        ev = BarEvents(resolution="ONE_MINUTE_CONSERVATIVE")
        low, high, close, ts = bar["low"], bar["high"], bar["close"], bar["timestamp_ny"]
        long = direction == "LONG"

        hit_entry = (
            not position_open and entry is not None
            and touches_entry(direction, low, high, entry)
        )
        in_pos = position_open or hit_entry
        hit_stop = in_pos and stop is not None and touches_stop(direction, low, high, stop)
        hit_target = in_pos and target is not None and touches_target(direction, low, high, target)

        if hit_entry:
            ev.sequence.append(("ENTRY", entry, ts))

        if hit_stop and hit_target:
            ev.ambiguity = (
                SequenceAmbiguity.ENTRY_STOP_TARGET_SAME_BAR
                if hit_entry
                else SequenceAmbiguity.STOP_AND_TARGET_SAME_BAR
            )
            ev.sequence.append(("STOP", stop, ts))
        elif hit_stop:
            if hit_entry:
                ev.ambiguity = SequenceAmbiguity.ENTRY_AND_STOP_SAME_BAR
            ev.sequence.append(("STOP", stop, ts))
        elif hit_target:
            if hit_entry:
                ev.ambiguity = SequenceAmbiguity.ENTRY_AND_TARGET_SAME_BAR
            ev.sequence.append(("TARGET", target, ts))

        # --- excursion consistent with the assumed path -------------------
        if not in_pos:
            return ev
        adverse_exit = hit_stop
        if adverse_exit:
            # assumed to run straight to the stop: no favourable credit
            anchor = entry if hit_entry else close
            ev.excursion_low, ev.excursion_high = (
                (stop, max(anchor, stop)) if long else (min(anchor, stop), stop)
            )
        elif hit_target:
            ev.excursion_low, ev.excursion_high = (
                (low, target) if long else (target, high)
            )
        elif hit_entry:
            # only what is provably after the fill
            ev.excursion_low, ev.excursion_high = (
                (low, max(entry, close)) if long else (min(entry, close), high)
            )
        else:
            ev.excursion_low, ev.excursion_high = low, high
        return ev

    # -- 1-second / tick ----------------------------------------------------

    def _resolve_finer(
        self, finer, *, direction, entry, stop, target, position_open, minute
    ) -> BarEvents:
        ev = BarEvents(resolution=self.mode)
        long = direction == "LONG"
        in_pos = position_open
        lo = hi = None

        for row in finer:
            r_low = row.get("low", row.get("price"))
            r_high = row.get("high", row.get("price"))
            ts = row.get("timestamp_ny") or row["timestamp_utc"]

            if not in_pos and entry is not None and touches_entry(direction, r_low, r_high, entry):
                ev.sequence.append(("ENTRY", entry, ts))
                in_pos = True
                lo = hi = entry
                # the rest of this row is still tradeable against the position
            if not in_pos:
                continue

            lo = r_low if lo is None else min(lo, r_low)
            hi = r_high if hi is None else max(hi, r_high)

            hit_stop = stop is not None and touches_stop(direction, r_low, r_high, stop)
            hit_target = target is not None and touches_target(direction, r_low, r_high, target)
            if hit_stop and hit_target:
                # both inside one second/tick: conservative again
                ev.ambiguity = SequenceAmbiguity.STOP_AND_TARGET_SAME_BAR
                ev.sequence.append(("STOP", stop, ts))
                lo, hi = (min(lo, stop), hi) if long else (lo, max(hi, stop))
                break
            if hit_stop:
                ev.sequence.append(("STOP", stop, ts))
                lo, hi = (min(lo, stop), hi) if long else (lo, max(hi, stop))
                break
            if hit_target:
                ev.sequence.append(("TARGET", target, ts))
                lo, hi = (lo, max(hi, target)) if long else (min(lo, target), hi)
                break

        if in_pos and lo is not None:
            ev.excursion_low, ev.excursion_high = lo, hi
        elif in_pos:
            ev.excursion_low = minute["low"]
            ev.excursion_high = minute["high"]
        return ev
