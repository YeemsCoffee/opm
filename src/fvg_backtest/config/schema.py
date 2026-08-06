"""Pydantic configuration schema.

Every strategy rule, threshold, and cost assumption from the research spec is
an editable field here.  Nothing strategy-relevant is hard-coded elsewhere:
instrument metadata, session times, FVG significance thresholds, liquidity
rules, entry/stop models, execution costs, and label definitions all flow
from this schema (loaded from YAML, editable in the dashboard).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# enums (kept as Literals so they serialize cleanly to YAML)
# ---------------------------------------------------------------------------

ContractMode = Literal["DATED", "CONTINUOUS"]
RollMethod = Literal[
    "HIGHEST_VOLUME",
    "FIXED_DAYS_BEFORE_EXPIRATION",
    "USER_DEFINED_ROLL_CALENDAR",
]
EntryModel = Literal["PROXIMAL_EDGE", "MIDPOINT", "DISTAL_EDGE"]
InversionStopModel = Literal[
    "OPPOSITE_FVG_EDGE_PLUS_BUFFER",
    "INVERSION_CANDLE_EXTREME_PLUS_BUFFER",
    "MOST_RECENT_SWING_PLUS_BUFFER",
    "ORIGINAL_CANDLE_1_EXTREME",
]
ExecutionMode = Literal["ONE_MINUTE_CONSERVATIVE", "ONE_SECOND_INTRABAR", "TICK_INTRABAR"]
BufferUnit = Literal["ticks", "points", "atr"]
PivotStrength = Literal[1, 2, 3]


class StrictModel(BaseModel):
    model_config = {"extra": "forbid", "validate_assignment": True}


# ---------------------------------------------------------------------------
# instruments
# ---------------------------------------------------------------------------


class CostConfig(StrictModel):
    """Per-instrument execution cost assumptions (NQ and MNQ may differ)."""

    commission_per_contract: float = 0.85  # broker commission, per side
    exchange_fees_per_contract: float = 1.64  # exchange + clearing + NFA, per side
    spread_ticks: float = 1.0  # assumed bid/ask spread paid on marketable exits
    entry_slippage_ticks: float = 0.0  # limit entries: 0 by default
    stop_slippage_ticks: float = 1.0
    target_slippage_ticks: float = 0.0


class DatabentoSymbolConfig(StrictModel):
    dataset: str = "GLBX.MDP3"
    parent_symbol: str = "NQ.FUT"  # stype_in="parent"
    continuous_symbol: str = "NQ.c.0"  # stype_in="continuous" (lead by volume)


class InstrumentConfig(StrictModel):
    """Editable NQ / MNQ contract metadata.

    Defaults follow CME specs but are configuration, not code: change them
    here (or in YAML) rather than in strategy logic.
    """

    root: str
    description: str = ""
    exchange: str = "CME"
    tick_size: float = 0.25  # minimum price increment, in index points
    point_value: float = 20.0  # $ per 1.00 index point per contract
    currency: str = "USD"
    contract_months: list[str] = Field(default_factory=lambda: ["H", "M", "U", "Z"])
    # expiration: 9:30 a.m. New York on the third Friday of the contract month
    expiration_rule: Literal["THIRD_FRIDAY_930"] = "THIRD_FRIDAY_930"
    trading_calendar: str = "CME_EQUITY_INDEX"
    databento: DatabentoSymbolConfig = Field(default_factory=DatabentoSymbolConfig)
    costs: CostConfig = Field(default_factory=CostConfig)

    @property
    def tick_value(self) -> float:
        return self.tick_size * self.point_value

    @field_validator("tick_size", "point_value")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("must be positive")
        return v


def default_instruments() -> dict[str, InstrumentConfig]:
    return {
        "NQ": InstrumentConfig(
            root="NQ",
            description="E-mini Nasdaq-100 futures",
            tick_size=0.25,
            point_value=20.0,
            databento=DatabentoSymbolConfig(
                dataset="GLBX.MDP3", parent_symbol="NQ.FUT", continuous_symbol="NQ.c.0"
            ),
            costs=CostConfig(
                commission_per_contract=0.85,
                exchange_fees_per_contract=1.64,
                spread_ticks=1.0,
                entry_slippage_ticks=0.0,
                stop_slippage_ticks=1.0,
                target_slippage_ticks=0.0,
            ),
        ),
        "MNQ": InstrumentConfig(
            root="MNQ",
            description="Micro E-mini Nasdaq-100 futures",
            tick_size=0.25,
            point_value=2.0,
            databento=DatabentoSymbolConfig(
                dataset="GLBX.MDP3", parent_symbol="MNQ.FUT", continuous_symbol="MNQ.c.0"
            ),
            costs=CostConfig(
                commission_per_contract=0.35,
                exchange_fees_per_contract=0.37,
                spread_ticks=1.0,
                stop_slippage_ticks=1.0,
                target_slippage_ticks=0.0,
            ),
        ),
    }


# ---------------------------------------------------------------------------
# data / sessions
# ---------------------------------------------------------------------------


class DataConfig(StrictModel):
    provider: Literal["databento", "csv", "parquet", "synthetic"] = "csv"
    path: str | None = None  # csv/parquet file or directory
    cache_dir: str = "data_cache"
    signal_resolution: Literal["1m"] = "1m"  # signals always use 1-minute candles
    execution_resolution: Literal["1m", "1s", "tick"] = "1m"
    validate_quality: bool = True
    # abort the run when validation finds errors (warnings never abort)
    fail_on_quality_errors: bool = False


class SessionConfig(StrictModel):
    timezone: str = "America/New_York"
    globex_session_start: str = "18:00"  # previous calendar day, NY time
    globex_session_end: str = "17:00"
    maintenance_break_start: str = "17:00"
    maintenance_break_end: str = "18:00"
    premarket_start: str = "04:00"  # start of the "premarket" sub-window
    cash_open: str = "09:30"
    cash_close: str = "16:00"
    fvg_search_start: str = "09:30"
    fvg_search_end: str = "16:00"
    trade_management_end: str = "16:00"
    include_globex_data_for_indicators: bool = True
    include_premarket_for_liquidity: bool = True

    @field_validator(
        "globex_session_start",
        "globex_session_end",
        "maintenance_break_start",
        "maintenance_break_end",
        "premarket_start",
        "cash_open",
        "cash_close",
        "fvg_search_start",
        "fvg_search_end",
        "trade_management_end",
    )
    @classmethod
    def _hhmm(cls, v: str) -> str:
        h, m = v.split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError(f"invalid HH:MM time: {v}")
        return v


class RollConfig(StrictModel):
    method: RollMethod = "HIGHEST_VOLUME"
    fixed_days_before_expiration: int = 8  # calendar days, FIXED_DAYS mode
    # user calendar: contract code -> ISO date of its LAST session as front
    roll_calendar: dict[str, str] = Field(default_factory=dict)
    # sessions within this many calendar days before the roll date are
    # classified ROLLOVER_TRANSITION so results can be compared
    rollover_window_days: int = 5
    exclude_rollover_sessions: bool = False
    exclude_expiration_week: bool = False
    back_adjust: bool = False  # WARNING: distorts price-level-dependent logic


# ---------------------------------------------------------------------------
# strategy: FVG detection & significance
# ---------------------------------------------------------------------------


class AtrConfig(StrictModel):
    timeframe: Literal["1m"] = "1m"
    length: int = 20
    method: Literal["WILDER", "SMA"] = "WILDER"


class FvgRuleConfig(StrictModel):
    # all three candles must start at/after fvg_search_start by default;
    # alternative test: only candle 3 must complete after the search start
    all_candles_after_open: bool = True
    strict_inequality: bool = True  # candle3.low > candle1.high (vs >=)
    respect_min_tick: bool = True  # gap must be >= one tick wide


class TypeAConfig(StrictModel):
    minimum_gap_atr: float = 0.10
    minimum_preservation_ratio: float = 0.50


class TypeBConfig(StrictModel):
    prior_wick_lookback_minutes: int = 15
    minimum_wick_atr: float = 0.15
    minimum_wick_share: float = 0.40
    minimum_fvg_overlap_ratio: float = 0.25


class ZoneConfig(StrictModel):
    # a close exactly on the boundary does NOT invert by default
    invert_on_touch_close: bool = False


# ---------------------------------------------------------------------------
# liquidity & targets
# ---------------------------------------------------------------------------


class LiquidityConfig(StrictModel):
    lookback_minutes: int = 60
    pivot_strength: PivotStrength = 1
    sweep_tolerance_ticks: float = 0.0  # trade beyond pivot by > tol => swept
    count_exact_touch_as_sweep: bool = False


class TargetConfig(StrictModel):
    # a pivot must have been formed at least this long before the FVG
    # confirmation candle to be an eligible target
    min_target_age_minutes: int = 5
    allow_touched_targets: bool = True  # touched-but-not-swept stay eligible
    max_lookback_minutes: int = 60


class EqualLevelsConfig(StrictModel):
    tolerance_mode: Literal["ticks", "atr"] = "ticks"
    tolerance_ticks: float = 2.0
    tolerance_atr: float = 0.02


class ContextConfig(StrictModel):
    opening_range_minutes_short: int = 5  # 9:30–9:35
    opening_range_minutes_long: int = 15  # 9:30–9:45
    whole_number_increment: float = 100.0  # "large" round level, in points
    round_number_increment: float = 25.0  # nearest configurable increment


# ---------------------------------------------------------------------------
# entries / orders / execution
# ---------------------------------------------------------------------------


class BufferConfig(StrictModel):
    unit: BufferUnit = "ticks"
    ticks: float = 4.0
    points: float = 1.0
    atr: float = 0.10


class EntryConfig(StrictModel):
    model: EntryModel = "PROXIMAL_EDGE"


class InversionConfig(StrictModel):
    enabled: bool = True
    entry_model: EntryModel = "PROXIMAL_EDGE"
    stop_model: InversionStopModel = "OPPOSITE_FVG_EDGE_PLUS_BUFFER"
    stop_buffer: BufferConfig = Field(default_factory=BufferConfig)
    max_reinversion_entries: int = 4  # cap re-inversion trades per session


class OrderConfig(StrictModel):
    max_order_age_minutes: int = 30  # unfilled orders expire after this
    cancel_when_target_swept: bool = True
    one_position_at_a_time: bool = True
    quantity: int = 1  # analysis stays normalized in R regardless


class ExecutionConfig(StrictModel):
    mode: ExecutionMode = "ONE_MINUTE_CONSERVATIVE"
    report_gross_and_net: bool = True


class ManagementConfig(StrictModel):
    full_exit_at_target: bool = True
    full_exit_at_stop: bool = True
    forced_exit_at_session_end: bool = True
    # (partials / breakeven / trailing intentionally unsupported)


# ---------------------------------------------------------------------------
# labels & range research
# ---------------------------------------------------------------------------


class CleanWinConfig(StrictModel):
    max_mae_r: float = 0.35
    max_entry_recross: int = 1
    reach_half_r_within_minutes: int = 5
    max_duration_minutes: int = 15


class SweatyWinConfig(StrictModel):
    min_conditions: int = 2
    mae_r_above: float = 0.50
    entry_cross_at_least: int = 3
    midpoint_cross_at_least: int = 3
    fail_half_r_within_minutes: int = 10
    duration_over_minutes: int = 20
    near_stop_after_half_r_fraction: float = 0.25  # within 25% of stop distance
    post_entry_overlap_above: float = 0.60


class ImmediateFailureConfig(StrictModel):
    max_mfe_r: float = 0.25  # stopped before MFE reaches this


class StalledConfig(StrictModel):
    after_minutes: int = 10
    mfe_r_below: float = 0.50


class RangingConfig(StrictModel):
    min_conditions: int = 2
    entry_cross_at_least: int = 3
    midpoint_cross_at_least: int = 3
    efficiency_ratio_bars: int = 10
    efficiency_ratio_below: float = 0.25
    net_progress_r_below: float = 0.50
    overlap_above: float = 0.60
    no_resolution_bars: int = 10
    direction_changes_at_least: int = 2


class LabelConfig(StrictModel):
    clean_win: CleanWinConfig = Field(default_factory=CleanWinConfig)
    sweaty_win: SweatyWinConfig = Field(default_factory=SweatyWinConfig)
    immediate_failure: ImmediateFailureConfig = Field(default_factory=ImmediateFailureConfig)
    stalled: StalledConfig = Field(default_factory=StalledConfig)
    ranging: RangingConfig = Field(default_factory=RangingConfig)


class RangeResearchConfig(StrictModel):
    efficiency_windows: list[int] = Field(default_factory=lambda: [5, 10, 20])
    onset_window_bars: int = 10
    onset_efficiency_below: float = 0.25
    onset_overlap_above: float = 0.60
    onset_net_progress_atr_below: float = 1.0


# ---------------------------------------------------------------------------
# walk-forward & models
# ---------------------------------------------------------------------------


class WalkForwardConfig(StrictModel):
    development_days: int = 120
    validation_days: int = 30
    out_of_sample_days: int = 30
    step_days: int = 30
    min_trades_per_fold: int = 15
    optimize_metric: Literal["expectancy_r", "net_expectancy_r", "profit_factor"] = (
        "expectancy_r"
    )
    # small default grid; edit freely
    grid: dict[str, list[float]] = Field(
        default_factory=lambda: {
            "significance.type_a.minimum_gap_atr": [0.05, 0.10, 0.20],
            "significance.type_a.minimum_preservation_ratio": [0.25, 0.50, 0.75],
        }
    )


class ModelsConfig(StrictModel):
    enabled: bool = False
    targets: list[str] = Field(
        default_factory=lambda: [
            "probability_target_before_stop",
            "probability_clean_win",
            "probability_sweaty_trade",
            "probability_ranging_trade",
        ]
    )
    algorithms: list[Literal["logistic", "hist_gradient_boosting"]] = Field(
        default_factory=lambda: ["logistic", "hist_gradient_boosting"]
    )
    train_fraction: float = 0.7  # chronological split


# ---------------------------------------------------------------------------
# top-level
# ---------------------------------------------------------------------------


class SignificanceConfig(StrictModel):
    type_a: TypeAConfig = Field(default_factory=TypeAConfig)
    type_b: TypeBConfig = Field(default_factory=TypeBConfig)


class AppConfig(StrictModel):
    """Top-level application configuration."""

    instrument: Literal["NQ", "MNQ"] = "NQ"
    contract_mode: ContractMode = "CONTINUOUS"
    contract: str | None = None  # e.g. "NQH25" when contract_mode == DATED
    start: str | None = None  # ISO date, inclusive
    end: str | None = None  # ISO date, inclusive

    instruments: dict[str, InstrumentConfig] = Field(default_factory=default_instruments)
    data: DataConfig = Field(default_factory=DataConfig)
    sessions: SessionConfig = Field(default_factory=SessionConfig)
    rolls: RollConfig = Field(default_factory=RollConfig)
    atr: AtrConfig = Field(default_factory=AtrConfig)
    fvg: FvgRuleConfig = Field(default_factory=FvgRuleConfig)
    significance: SignificanceConfig = Field(default_factory=SignificanceConfig)
    zone: ZoneConfig = Field(default_factory=ZoneConfig)
    liquidity: LiquidityConfig = Field(default_factory=LiquidityConfig)
    targets: TargetConfig = Field(default_factory=TargetConfig)
    equal_levels: EqualLevelsConfig = Field(default_factory=EqualLevelsConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    entries: EntryConfig = Field(default_factory=EntryConfig)
    inversion: InversionConfig = Field(default_factory=InversionConfig)
    orders: OrderConfig = Field(default_factory=OrderConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    management: ManagementConfig = Field(default_factory=ManagementConfig)
    labels: LabelConfig = Field(default_factory=LabelConfig)
    range_research: RangeResearchConfig = Field(default_factory=RangeResearchConfig)
    walkforward: WalkForwardConfig = Field(default_factory=WalkForwardConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    runs_dir: str = "runs"

    @model_validator(mode="after")
    def _check(self) -> "AppConfig":
        if self.contract_mode == "DATED" and not self.contract:
            raise ValueError("contract_mode=DATED requires `contract` (e.g. NQH25)")
        if self.instrument not in self.instruments:
            raise ValueError(f"instrument {self.instrument!r} missing from instruments map")
        return self

    @property
    def active_instrument(self) -> InstrumentConfig:
        return self.instruments[self.instrument]

    def get_by_path(self, dotted: str):
        obj = self
        for part in dotted.split("."):
            obj = getattr(obj, part)
        return obj

    def set_by_path(self, dotted: str, value) -> None:
        parts = dotted.split(".")
        obj = self
        for part in parts[:-1]:
            obj = getattr(obj, part)
        setattr(obj, parts[-1], value)
