"""Deterministic synthetic NQ/MNQ sessions with planted, provable setups.

Each scenario builds one full Globex session (18:00 prev day -> 17:00 NY) of
one-minute candles around a base price ``B``.  Prices are expressed as
*offsets* from ``B`` in the bullish frame; bearish scenarios mirror every
offset, so both sides are exercised by identical geometry.

Planted structure common to every FVG scenario:

===========  ==============================================================
08:55        spike bar, high ``B+18``  -> the bullish swing-high target
09:05        spike bar, low  ``B-15``  -> the bearish swing-low target
09:22        wick bar into the future zone -> satisfies Type B
09:30–09:33  the first significant FVG: zone ``[B+5, B+9]``, stop ``B+1``
===========  ==============================================================

Every *generated* (non-planted) bar gets wicks of
``0.3 + max(|step_in|, |step_out|)``.  That guarantees no accidental FVG can
form between generated bars: a bullish gap needs
``min(o3,c3) - max(o1,c1) > r1 + r3``, but the left side is at most
``|c2 - c1|`` while ``r1 >= |c2 - c1| + 0.3``.  The planted pattern is
therefore provably the session's *first presented* FVG.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import polars as pl

from ..config.schema import InstrumentConfig
from ..sessions.clock import SessionClock
from .provider import MarketDataProvider
from .schema import normalize_candles

UTC = timezone.utc

SCENARIOS = (
    "bullish_clean",
    "bearish_clean",
    "bullish_inversion",
    "ranging",
    "sweaty_win",
    "no_fvg",
)

# minute index = minutes since the 18:00 NY Globex open; 09:30 == 930
M930 = 930
SESSION_MINUTES = 1380  # 18:00 -> 17:00 next day
WICK_COEFF = 1.0  # >= 1.0 keeps the no-accidental-FVG proof valid
WICK_FLOOR = 0.3


@dataclass(frozen=True)
class Bar:
    """Planted OHLC, as offsets from the base price (bullish frame)."""

    o: float
    h: float
    l: float
    c: float

    def resolve(self, base: float, sgn: int) -> tuple[float, float, float, float]:
        o, h, l, c = (base + sgn * v for v in (self.o, self.h, self.l, self.c))
        if sgn < 0:
            h, l = l, h
        return o, h, l, c


def _rt(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)


def _interp(waypoints: list[tuple[int, float]], minute: int) -> float:
    if minute <= waypoints[0][0]:
        return waypoints[0][1]
    for (m0, p0), (m1, p1) in zip(waypoints, waypoints[1:]):
        if m0 <= minute <= m1:
            return p0 if m1 == m0 else p0 + (p1 - p0) * (minute - m0) / (m1 - m0)
    return waypoints[-1][1]


def _wave(minute: int, amp: float) -> float:
    return amp * math.sin(minute * 0.61) + 0.4 * amp * math.sin(minute * 0.173)


def _volume(minute: int) -> float:
    if minute < M930:
        return 60 + 40 * math.sin(minute * 0.05) ** 2
    if minute < M930 + 30:
        return 2600 - 50 * (minute - M930)
    if minute < 1320:
        return 700 + 250 * math.sin(minute * 0.03) ** 2
    return 300


# ---------------------------------------------------------------------------
# planted structure (bullish frame, offsets from B)
# ---------------------------------------------------------------------------

# overnight -> premarket close path
_PREMARKET_WAY: list[tuple[int, float]] = [
    (0, -40), (120, -10), (240, 25), (420, 5), (660, -30), (780, -12),
    (840, 4), (860, 3), (890, 2),
    (895, 2.75),    # planted spike-high bar closes here
    (900, 0.5),
    (905, -3.5),    # planted spike-low bar closes here
    (906, -3), (915, 1), (918, 2),
    (922, 6.25),    # planted Type-B wick bar closes here
    (926, 3), (929, 2),
]

_PREMARKET_BARS: dict[int, Bar] = {
    895: Bar(2.5, 18.0, 1.5, 2.75),      # swing high  @ B+18 (bullish target)
    905: Bar(0.5, 1.0, -15.0, -3.5),     # swing low   @ B-15 (bearish target)
    922: Bar(6.0, 8.5, 5.75, 6.25),      # prior wick overlapping the zone
}

# the first presented FVG: zone [B+5, B+9], candle-1 low B+1 (the stop)
_FVG_BARS: dict[int, Bar] = {
    930: Bar(2.0, 4.0, -0.5, 2.25),      # deliberately blocks a 930/931/932 gap
    931: Bar(2.0, 5.0, 1.0, 4.0),        # candle 1
    932: Bar(4.0, 13.0, 3.75, 12.0),     # candle 2 (displacement)
    933: Bar(10.5, 15.0, 9.0, 11.5),     # candle 3 -> gap [B+5, B+9]
}


def _scenario(kind: str) -> tuple[list[tuple[int, float]], dict[int, Bar], int]:
    """Return (cash waypoints, planted cash bars, sign) in the bullish frame."""
    sgn = -1 if kind == "bearish_clean" else 1
    if kind in ("bullish_clean", "bearish_clean"):
        planted = {
            **_FVG_BARS,
            938: Bar(9.5, 10.0, 8.5, 9.75),       # limit fill at B+9
            942: Bar(12.5, 13.75, 12.25, 13.5),   # 0.5R within 5 minutes
            947: Bar(17.0, 18.75, 16.5, 17.5),    # target B+18 reached
        }
        way = [
            (934, 11.0), (936, 10.4), (937, 10.0), (939, 10.5), (941, 12.0),
            (943, 14.0), (946, 16.8), (948, 17.0), (955, 16.0), (1010, 17.0),
            (1100, 15.5), (1200, 16.5), (1320, 14.0), (SESSION_MINUTES - 1, 14.0),
        ]
    elif kind == "bullish_inversion":
        planted = {
            **_FVG_BARS,
            936: Bar(10.0, 10.5, 6.5, 7.0),       # fill + mitigation
            937: Bar(7.0, 7.5, 0.75, 3.0),        # stop, closes below B+5 => inversion
            940: Bar(4.0, 5.5, 3.5, 4.0),         # inversion short fills at B+5
            962: Bar(-13.0, -12.0, -15.75, -14.0),  # target: the 09:05 low B-15
        }
        way = [
            (934, 11.0), (935, 10.2), (938, 3.2), (939, 4.0), (943, 2.5),
            (947, 1.0), (951, -3.0), (956, -8.0), (960, -11.5), (965, -12.0),
            (990, -10.0), (1100, -8.0), (1320, -10.0), (SESSION_MINUTES - 1, -10.0),
        ]
    elif kind == "ranging":
        planted = {
            **_FVG_BARS,
            936: Bar(10.0, 10.25, 8.5, 9.25),     # fill
            1050: Bar(1.5, 2.0, 0.5, 1.25),       # late stop-out at B+1
        }
        way = [
            (934, 10.6), (940, 10.5), (944, 7.5), (948, 10.5), (952, 6.8),
            (956, 10.2), (960, 7.2), (965, 9.8), (970, 6.5), (976, 9.6),
            (982, 6.9), (988, 9.4), (1000, 6.0), (1020, 4.0), (1040, 2.6),
            (1055, 2.0), (1100, 2.5), (1320, 2.0), (SESSION_MINUTES - 1, 2.0),
        ]
    elif kind == "sweaty_win":
        # a winner that takes 0.85R of heat, recrosses the entry repeatedly
        # and needs half an hour: every slope stays gentle enough that no
        # generated wick can reach the B+1 stop
        planted = {
            **_FVG_BARS,
            936: Bar(10.5, 10.75, 9.5, 10.0),     # approaches without filling
            937: Bar(9.5, 9.75, 8.5, 9.0),        # fill at B+9
            943: Bar(3.0, 4.0, 2.2, 3.5),         # deep MAE (0.85R), stop survives
            1008: Bar(16.0, 18.75, 15.5, 17.0),   # late target
        }
        way = [
            (934, 11.0), (936, 10.0), (937, 9.0), (939, 7.0), (941, 5.0),
            (943, 3.5), (945, 5.0), (947, 6.5), (949, 8.0), (951, 9.5),
            (953, 8.2), (955, 9.6), (957, 8.4), (959, 9.8), (962, 8.6),
            (966, 10.0), (975, 9.2), (990, 11.0), (1000, 12.5), (1004, 14.0),
            (1008, 17.0), (1012, 16.0), (1060, 15.0), (1320, 13.0),
            (SESSION_MINUTES - 1, 13.0),
        ]
    elif kind == "no_fvg":
        planted = {}
        way = [
            (935, 1.0), (960, 3.0), (1000, -2.0), (1060, 2.0), (1130, -1.0),
            (1200, 1.0), (1320, 0.0), (SESSION_MINUTES - 1, 0.0),
        ]
    else:
        raise ValueError(f"unknown scenario {kind!r}; choose from {SCENARIOS}")
    return way, planted, sgn


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------


def generate_session_bars(
    session_date: date,
    kind: str,
    clock: SessionClock,
    *,
    base_price: float = 21000.0,
    tick: float = 0.25,
    contract: str = "NQH25",
) -> pl.DataFrame:
    """One full Globex session of 1-minute bars (raw columns, UTC stamps)."""
    cash_way, cash_bars, sgn = _scenario(kind)
    planted = {**_PREMARKET_BARS, **cash_bars}
    waypoints = _PREMARKET_WAY + cash_way

    # actual close path (planted closes substituted) so each generated bar
    # sizes its wicks against its true neighbours
    closes = [
        _interp(waypoints, m) + (_wave(m, 0.8) if m < 880 else 0.0)
        for m in range(SESSION_MINUTES)
    ]
    for m, bar in planted.items():
        closes[m] = bar.c

    open_ny, _ = clock.session_bounds(session_date)
    open_utc = open_ny.astimezone(UTC)

    rows: list[dict] = []
    prev_close_off = closes[0]
    for m in range(SESSION_MINUTES):
        ts = open_utc + timedelta(minutes=m)
        if m in planted:
            o, h, l, c = planted[m].resolve(base_price, sgn)
            prev_close_off = planted[m].c
        else:
            c_off = closes[m]
            o_off = prev_close_off
            step = abs(c_off - o_off)
            nxt = abs(closes[m + 1] - c_off) if m + 1 < SESSION_MINUTES else step
            r = WICK_FLOOR + WICK_COEFF * max(step, nxt)
            hi_off, lo_off = max(o_off, c_off) + r, min(o_off, c_off) - r
            o, c = base_price + sgn * o_off, base_price + sgn * c_off
            h, l = base_price + sgn * hi_off, base_price + sgn * lo_off
            if sgn < 0:
                h, l = l, h
            prev_close_off = c_off
        o, h, l, c = (_rt(x, tick) for x in (o, h, l, c))
        h, l = max(h, o, c), min(l, o, c)
        vol = round(_volume(m))
        rows.append(
            {
                "timestamp_utc": ts,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": float(vol),
                "trade_count": int(vol // 3) + 1,
                "vwap": _rt((h + l + 2 * c) / 4, tick),
                "underlying_contract": contract,
            }
        )
    return pl.DataFrame(rows)


def explode_minute_to_seconds(
    bar: dict, *, tick: float = 0.25, sequence: str | None = None
) -> list[dict]:
    """Deterministic 1-second path inside a minute bar.

    ``sequence``: ``"OHLC"`` visits the high first, ``"OLHC"`` the low first.
    Default: red candles go high-first, green candles low-first, matching the
    conservative adverse-order assumption so 1-second replays agree with
    1-minute conservative mode unless a test overrides the sequence.
    """
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    if sequence is None:
        sequence = "OHLC" if c < o else "OLHC"
    pts = [o, h, l, c] if sequence == "OHLC" else [o, l, h, c]
    path: list[float] = []
    for leg in range(3):
        a, b = pts[leg], pts[leg + 1]
        path.extend(a + (b - a) * (s / 19) for s in range(20))

    out: list[dict] = []
    ts0 = bar["timestamp_utc"]
    for s in range(60):
        so = _rt(path[s], tick)
        sc = _rt(c if s == 59 else path[min(s + 1, 59)], tick)
        out.append(
            {
                "timestamp_utc": ts0 + timedelta(seconds=s),
                "open": so,
                "high": max(so, sc),
                "low": min(so, sc),
                "close": sc,
                "volume": max(1.0, bar["volume"] / 60.0),
                "trade_count": 1,
                "vwap": sc,
                "underlying_contract": bar["underlying_contract"],
            }
        )
    # pin exact extremes so the seconds aggregate back to the minute exactly
    hi_idx, lo_idx = (19, 39) if sequence == "OHLC" else (39, 19)
    out[hi_idx]["high"] = _rt(h, tick)
    out[lo_idx]["low"] = _rt(l, tick)
    out[0]["open"] = _rt(o, tick)
    out[59]["close"] = _rt(c, tick)
    for r in out:
        r["high"] = max(r["high"], r["open"], r["close"])
        r["low"] = min(r["low"], r["open"], r["close"])
    return out


def bars_to_ticks(second_rows: list[dict]) -> pl.DataFrame:
    """One trade per second bar (its close) — a minimal deterministic tape."""
    return pl.DataFrame(
        [
            {
                "timestamp_utc": r["timestamp_utc"],
                "price": r["close"],
                "size": max(1, int(r["volume"])),
                "underlying_contract": r["underlying_contract"],
            }
            for r in second_rows
        ]
    )


class SyntheticProvider(MarketDataProvider):
    """Provider serving deterministic scenario sessions.

    ``schedule`` maps session dates to scenario names; unlisted trading days
    cycle through :data:`SCENARIOS` in order.
    """

    name = "synthetic"

    def __init__(
        self,
        clock: SessionClock,
        instruments: dict[str, InstrumentConfig],
        schedule: dict[date, str] | None = None,
        base_price: float = 21000.0,
        second_sequences: dict[tuple[date, int], str] | None = None,
    ) -> None:
        self.clock = clock
        self.instruments = instruments
        self.schedule = schedule or {}
        self.base_price = base_price
        self.second_sequences = second_sequences or {}

    def scenario_for(self, session_date: date, i: int) -> str:
        return self.schedule.get(session_date, SCENARIOS[i % len(SCENARIOS)])

    def _sessions(self, start: datetime, end: datetime) -> list[date]:
        tz = self.clock.tz
        return self.clock.calendar.trading_days(
            start.astimezone(tz).date(), end.astimezone(tz).date()
        )

    def _raw(self, symbol: str, start, end, contract_mode: str):
        from ..futures.contracts import list_contracts, parse_contract

        if contract_mode == "DATED":
            contract = parse_contract(symbol)
            root, code = contract.root, contract.code
        else:
            root = symbol.upper()
            tz = self.clock.tz
            live = list_contracts(
                self.instruments[root], start.astimezone(tz).date(), end.astimezone(tz).date()
            )
            code = live[1].code if len(live) > 1 else live[0].code
        tick = self.instruments[root].tick_size
        frames = [
            generate_session_bars(
                d, self.scenario_for(d, i), self.clock,
                base_price=self.base_price, tick=tick, contract=code,
            )
            for i, d in enumerate(self._sessions(start, end))
        ]
        if not frames:
            raise ValueError("requested range contains no trading days")
        raw = pl.concat(frames).filter(
            (pl.col("timestamp_utc") >= start) & (pl.col("timestamp_utc") < end)
        )
        return raw, root, tick

    def _seconds(self, raw: pl.DataFrame, tick: float) -> list[dict]:
        rows: list[dict] = []
        for bar in raw.iter_rows(named=True):
            ny = bar["timestamp_utc"].astimezone(self.clock.tz)
            key = (
                self.clock.globex_session_date(bar["timestamp_utc"]),
                ny.hour * 60 + ny.minute,
            )
            rows.extend(
                explode_minute_to_seconds(
                    bar, tick=tick, sequence=self.second_sequences.get(key)
                )
            )
        return rows

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str,
        contract_mode: str,
    ) -> pl.DataFrame:
        raw, root, tick = self._raw(symbol, start, end, contract_mode)
        if timeframe == "1m":
            return normalize_candles(
                raw, clock=self.clock, symbol=symbol.upper(), root_symbol=root,
                source=self.name, resolution="1m",
            )
        if timeframe == "1s":
            return normalize_candles(
                pl.DataFrame(self._seconds(raw, tick)), clock=self.clock,
                symbol=symbol.upper(), root_symbol=root, source=self.name,
                resolution="1s",
            )
        if timeframe == "tick":
            return bars_to_ticks(self._seconds(raw, tick))
        raise ValueError(f"unsupported timeframe {timeframe!r}")
