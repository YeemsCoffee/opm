"""Target selection: the nearest eligible untaken liquidity.

Bullish trades aim at confirmed, unswept **swing highs above the entry**;
bearish trades at **swing lows below it**.  The search window is the trailing
``lookback_minutes`` measured at the moment of the decision, so it slides as
the session progresses.

The *closest by price distance* is selected — never the newest.  When no
eligible level exists the setup is labelled ``NO_TARGET``; a target is never
invented.

Equal highs/lows are grouped into clusters using a configurable tolerance
(ticks or ATR), and the selected target is annotated with the cluster it
belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..config.schema import EqualLevelsConfig, LiquidityConfig, TargetConfig
from .pivots import Pivot, PivotSide, PivotTracker

NO_TARGET = "NO_TARGET"


@dataclass
class EqualLevelCluster:
    price: float           # cluster reference (the extreme-most level)
    pivots: list[Pivot]
    spread: float

    @property
    def size(self) -> int:
        return len(self.pivots)

    def oldest_age(self, now: datetime) -> int:
        return max(p.age_minutes(now) for p in self.pivots)

    def newest_age(self, now: datetime) -> int:
        return min(p.age_minutes(now) for p in self.pivots)

    @property
    def retest_count(self) -> int:
        return sum(p.touch_count for p in self.pivots)


@dataclass
class TargetSelection:
    found: bool
    pivot: Pivot | None = None
    price: float | None = None
    label: str = NO_TARGET
    cluster: EqualLevelCluster | None = None
    intervening_levels: int = 0
    candidates: list[Pivot] = field(default_factory=list)

    def to_dict(self, *, now: datetime, entry: float, tick: float, atr: float, risk: float) -> dict:
        if not self.found or self.pivot is None or self.price is None:
            return {
                "target_found": False,
                "target_label": NO_TARGET,
                "target_price": None,
                "target_age_minutes": None,
                "target_distance_points": None,
                "target_distance_ticks": None,
                "target_distance_atr": None,
                "target_distance_r": None,
                "target_pivot_strength": None,
                "target_retest_count": None,
                "target_session_segment": None,
                "target_is_cluster": None,
                "target_cluster_size": None,
                "target_cluster_spread": None,
                "target_cluster_oldest_age": None,
                "target_cluster_newest_age": None,
                "target_intervening_levels": None,
                "target_candidate_count": len(self.candidates),
            }
        distance = abs(self.price - entry)
        return {
            "target_found": True,
            "target_label": self.label,
            "target_price": self.price,
            "target_age_minutes": self.pivot.age_minutes(now),
            "target_distance_points": distance,
            "target_distance_ticks": distance / tick if tick else 0.0,
            "target_distance_atr": distance / atr if atr > 0 else 0.0,
            "target_distance_r": distance / risk if risk > 0 else 0.0,
            "target_pivot_strength": self.pivot.strength,
            "target_retest_count": self.pivot.touch_count,
            "target_session_segment": self.pivot.session_segment,
            "target_is_cluster": bool(self.cluster and self.cluster.size > 1),
            "target_cluster_size": self.cluster.size if self.cluster else 1,
            "target_cluster_spread": self.cluster.spread if self.cluster else 0.0,
            "target_cluster_oldest_age": self.cluster.oldest_age(now) if self.cluster else None,
            "target_cluster_newest_age": self.cluster.newest_age(now) if self.cluster else None,
            "target_intervening_levels": self.intervening_levels,
            "target_candidate_count": len(self.candidates),
        }


def _tolerance(config: EqualLevelsConfig, tick: float, atr: float) -> float:
    if config.tolerance_mode == "atr":
        return config.tolerance_atr * atr
    return config.tolerance_ticks * tick


def build_clusters(
    pivots: list[Pivot], config: EqualLevelsConfig, tick: float, atr: float
) -> list[EqualLevelCluster]:
    """Group pivots whose prices sit within tolerance of each other."""
    if not pivots:
        return []
    tol = _tolerance(config, tick, atr)
    ordered = sorted(pivots, key=lambda p: p.price)
    clusters: list[list[Pivot]] = [[ordered[0]]]
    for p in ordered[1:]:
        if abs(p.price - clusters[-1][-1].price) <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    out = []
    for group in clusters:
        prices = [p.price for p in group]
        side = group[0].side
        ref = max(prices) if side == PivotSide.HIGH else min(prices)
        out.append(EqualLevelCluster(price=ref, pivots=group, spread=max(prices) - min(prices)))
    return out


def select_target(
    tracker: PivotTracker,
    *,
    direction: str,            # LONG | SHORT
    entry: float,
    now: datetime,
    liquidity_config: LiquidityConfig,
    target_config: TargetConfig,
    equal_levels: EqualLevelsConfig,
    tick: float,
    atr: float,
) -> TargetSelection:
    side = PivotSide.HIGH if direction == "LONG" else PivotSide.LOW
    lookback = min(target_config.max_lookback_minutes, liquidity_config.lookback_minutes)
    candidates = tracker.eligible_pivots(
        side,
        now,
        lookback_minutes=lookback,
        min_age_minutes=target_config.min_target_age_minutes,
        allow_touched=target_config.allow_touched_targets,
    )
    # only levels the trade can actually reach
    if direction == "LONG":
        reachable = [p for p in candidates if p.price > entry]
    else:
        reachable = [p for p in candidates if p.price < entry]
    if not reachable:
        return TargetSelection(found=False, candidates=candidates)

    # closest by positive price distance — never simply the newest
    chosen = min(reachable, key=lambda p: (abs(p.price - entry), p.timestamp))
    clusters = build_clusters(reachable, equal_levels, tick, atr)
    cluster = next((c for c in clusters if chosen in c.pivots), None)

    # any other confirmed liquidity — either side — sitting between the entry
    # and the target, i.e. something the trade has to trade through
    lo, hi = sorted((entry, chosen.price))
    in_cluster = set(id(p) for p in cluster.pivots) if cluster else set()
    intervening = sum(
        1
        for p in tracker.pivots
        if p is not chosen
        and id(p) not in in_cluster
        and p.confirmed_at <= now
        and lo < p.price < hi
    )
    return TargetSelection(
        found=True,
        pivot=chosen,
        price=chosen.price,
        label="SWING_HIGH" if side == PivotSide.HIGH else "SWING_LOW",
        cluster=cluster,
        intervening_levels=intervening,
        candidates=candidates,
    )
