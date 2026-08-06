"""Execution costs and tick rounding.

Gross and net results are always reported separately.  Net applies, per
contract and per side:

- commission + exchange/clearing fees (round turn = 2 x per-side)
- slippage in ticks, configurable per event type (entry / stop / target)
- half the assumed spread on marketable exits (stop / session-end),
  never on the limit entry

Prices are rounded to the instrument's tick.  Slippage always works
*against* the trade — a stop fills worse, a target no better.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.schema import InstrumentConfig


@dataclass
class CostModel:
    instrument: InstrumentConfig

    @property
    def tick(self) -> float:
        return self.instrument.tick_size

    @property
    def point_value(self) -> float:
        return self.instrument.point_value

    @property
    def tick_value(self) -> float:
        return self.instrument.tick_value

    def round_to_tick(self, price: float) -> float:
        return round(round(price / self.tick) * self.tick, 10)

    def round_up(self, price: float) -> float:
        import math

        return round(math.ceil(price / self.tick - 1e-9) * self.tick, 10)

    def round_down(self, price: float) -> float:
        import math

        return round(math.floor(price / self.tick + 1e-9) * self.tick, 10)

    # -- fills -------------------------------------------------------------

    def _adverse(self, price: float, direction: str, offset: float, entering: bool) -> float:
        """Apply an adverse offset and round *away* from the trade.

        Rounding to nearest would hand back sub-tick costs (a half-tick
        spread on an on-tick level would round to no cost at all), so an
        adverse adjustment always lands on the next worse tick.
        """
        pays_up = (direction == "LONG") if entering else (direction == "SHORT")
        shifted = price + offset if pays_up else price - offset
        return self.round_up(shifted) if pays_up else self.round_down(shifted)

    def entry_fill(self, price: float, direction: str) -> float:
        """Limit entry: slippage (if configured) makes the fill worse."""
        slip = self.instrument.costs.entry_slippage_ticks * self.tick
        return self._adverse(price, direction, slip, entering=True)

    def stop_fill(self, price: float, direction: str) -> float:
        """Stop exits pay slippage plus half the spread, always adverse."""
        adverse = (
            self.instrument.costs.stop_slippage_ticks
            + self.instrument.costs.spread_ticks / 2
        ) * self.tick
        return self._adverse(price, direction, adverse, entering=False)

    def target_fill(self, price: float, direction: str) -> float:
        """Resting limit target: slippage only if explicitly configured."""
        slip = self.instrument.costs.target_slippage_ticks * self.tick
        return self._adverse(price, direction, slip, entering=False)

    def market_exit_fill(self, price: float, direction: str) -> float:
        """Forced session-end exit: crosses the spread."""
        adverse = (self.instrument.costs.spread_ticks / 2) * self.tick
        return self._adverse(price, direction, adverse, entering=False)

    # -- money -------------------------------------------------------------

    @property
    def round_turn_fees(self) -> float:
        c = self.instrument.costs
        return 2 * (c.commission_per_contract + c.exchange_fees_per_contract)

    def points_to_dollars(self, points: float, quantity: int = 1) -> float:
        return points * self.point_value * quantity

    def net_dollars(self, gross_points: float, quantity: int = 1) -> float:
        return self.points_to_dollars(gross_points, quantity) - self.round_turn_fees * quantity

    def fees_in_points(self, quantity: int = 1) -> float:
        """Round-turn fees expressed in index points (for R accounting)."""
        return self.round_turn_fees / self.point_value
