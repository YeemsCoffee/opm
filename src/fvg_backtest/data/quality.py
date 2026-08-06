"""Data-quality validation for normalized candles.

Checks (spec §5): duplicate timestamps, missing bars, out-of-order bars,
invalid OHLC relationships, mid-session contract changes, DST transitions,
holidays, early closes, abnormally long gaps, zero-volume bars, and a
timezone sanity heuristic.  ERRORs indicate data that would corrupt
research; WARNINGs/INFOs are context the researcher should see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import polars as pl

from ..sessions.calendar import TradingCalendar
from ..sessions.clock import SessionClock, hhmm_to_minutes

_RES_SECONDS = {"1m": 60, "1s": 1}


@dataclass
class QualityIssue:
    check: str
    severity: str  # ERROR | WARNING | INFO
    message: str
    count: int = 0
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "count": self.count,
            "examples": self.examples[:8],
        }


@dataclass
class DataQualityReport:
    issues: list[QualityIssue] = field(default_factory=list)
    rows: int = 0
    sessions: int = 0

    @property
    def errors(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity == "WARNING"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, *args, **kwargs) -> None:
        self.issues.append(QualityIssue(*args, **kwargs))

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "sessions": self.sessions,
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
        }


def _fmt_ts(values: pl.Series, limit: int = 8) -> list[str]:
    return [str(v) for v in values.head(limit).to_list()]


def validate_candles(
    df: pl.DataFrame,
    clock: SessionClock,
    *,
    long_gap_minutes: int = 10,
) -> DataQualityReport:
    report = DataQualityReport(rows=df.height)
    if df.is_empty():
        report.add("empty", "ERROR", "no candles supplied")
        return report

    cal: TradingCalendar = clock.calendar
    res = df["resolution"][0]
    res_s = _RES_SECONDS.get(res)

    report.sessions = df["globex_session_date"].n_unique()

    # --- out-of-order (input order, per contract) ---------------------------
    unordered = (
        df.with_columns(pl.col("timestamp_utc").diff().alias("_d"))
        .filter(pl.col("_d") < 0)
    )
    if unordered.height:
        report.add(
            "out_of_order", "ERROR",
            "bars are not in ascending timestamp order (sorted automatically "
            "downstream, but the source should be checked)",
            unordered.height, _fmt_ts(unordered["timestamp_utc"]),
        )
        df = df.sort("timestamp_utc")

    # --- duplicates (same contract + timestamp) -----------------------------
    dupes = (
        df.group_by(["underlying_contract", "timestamp_utc"])
        .len()
        .filter(pl.col("len") > 1)
    )
    if dupes.height:
        report.add(
            "duplicate_timestamps", "ERROR",
            "duplicate (contract, timestamp) rows",
            dupes.height, _fmt_ts(dupes["timestamp_utc"]),
        )

    # --- OHLC relationship validity -----------------------------------------
    bad = df.filter(
        (pl.col("high") < pl.col("low"))
        | (pl.col("high") < pl.col("open"))
        | (pl.col("high") < pl.col("close"))
        | (pl.col("low") > pl.col("open"))
        | (pl.col("low") > pl.col("close"))
        | (pl.col("open") <= 0)
        | (pl.col("close") <= 0)
    )
    if bad.height:
        report.add(
            "invalid_ohlc", "ERROR", "rows violate OHLC relationships",
            bad.height, _fmt_ts(bad["timestamp_ny"]),
        )

    # --- contract changes ----------------------------------------------------
    changes = (
        df.sort("timestamp_utc")
        .with_columns(pl.col("underlying_contract").shift(1).alias("_prev"))
        .filter(
            pl.col("_prev").is_not_null()
            & (pl.col("underlying_contract") != pl.col("_prev"))
        )
    )
    if changes.height:
        # a contract change is fine at a session boundary (that's a roll);
        # inside one Globex session it means mixed data
        intra = (
            df.group_by("globex_session_date")
            .agg(pl.col("underlying_contract").n_unique().alias("n"))
            .filter(pl.col("n") > 1)
        )
        if intra.height:
            report.add(
                "contract_change_mid_session", "ERROR",
                "sessions contain more than one underlying contract",
                intra.height,
                [str(d) for d in intra["globex_session_date"].head(8).to_list()],
            )
        report.add(
            "contract_changes", "INFO",
            "contract roll boundaries present in the series",
            changes.height, _fmt_ts(changes["timestamp_ny"]),
        )

    # --- missing bars / long gaps (within sessions, tradeable segments) -----
    if res_s:
        maint_start = hhmm_to_minutes(clock.config.maintenance_break_start)
        maint_end = hhmm_to_minutes(clock.config.maintenance_break_end)
        gaps = (
            df.sort("timestamp_utc")
            .with_columns(
                (pl.col("timestamp_utc").diff().dt.total_seconds()).alias("_gap"),
                pl.col("globex_session_date").shift(1).alias("_prev_sess"),
                (
                    pl.col("timestamp_ny").dt.hour().cast(pl.Int32) * 60
                    + pl.col("timestamp_ny").dt.minute().cast(pl.Int32)
                ).alias("_mod"),
            )
            .filter(
                (pl.col("globex_session_date") == pl.col("_prev_sess"))
                & (pl.col("_gap") > res_s)
                # ignore the daily maintenance break re-open bar
                & ~((pl.col("_mod") >= maint_end) & (pl.col("_gap") <= (maint_end - maint_start) * 60 + res_s))
            )
        )
        if gaps.height:
            missing_est = int((gaps["_gap"].sum() - res_s * gaps.height) // res_s)
            report.add(
                "missing_bars", "WARNING",
                f"gaps between consecutive bars within sessions (~{missing_est} bars missing; "
                "zero-trade minutes are normal in Globex hours)",
                gaps.height, _fmt_ts(gaps["timestamp_ny"]),
            )
            long_gaps = gaps.filter(pl.col("_gap") > long_gap_minutes * 60)
            if long_gaps.height:
                report.add(
                    "abnormally_long_gaps", "WARNING",
                    f"gaps longer than {long_gap_minutes} minutes inside sessions",
                    long_gaps.height, _fmt_ts(long_gaps["timestamp_ny"]),
                )

    # --- holiday / weekend bars ----------------------------------------------
    sess_dates = df["globex_session_date"].unique().to_list()
    closed = [d for d in sess_dates if cal.is_closed(d)]
    if closed:
        report.add(
            "holiday_sessions", "WARNING",
            "bars assigned to closed dates (holiday Globex trade); these "
            "sessions are excluded from signal research",
            len(closed), [str(d) for d in closed[:8]],
        )
    early = [d for d in sess_dates if isinstance(d, date) and cal.is_early_close(d)]
    if early:
        report.add(
            "early_closes", "INFO", "early-close sessions in range",
            len(early), [str(d) for d in early[:8]],
        )

    # --- DST transitions ------------------------------------------------------
    dst = (
        df.with_columns(pl.col("timestamp_ny").dt.dst_offset().alias("_dst"))
        .group_by("globex_session_date")
        .agg(pl.col("_dst").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
    )
    if dst.height:
        report.add(
            "dst_transition", "INFO",
            "sessions spanning a daylight-saving transition (bar counts differ)",
            dst.height, [str(d) for d in dst["globex_session_date"].head(8).to_list()],
        )

    # --- zero volume -----------------------------------------------------------
    zero_vol = df.filter(pl.col("volume") == 0)
    if zero_vol.height:
        report.add(
            "zero_volume_bars", "INFO", "bars with zero volume",
            zero_vol.height, _fmt_ts(zero_vol["timestamp_ny"]),
        )

    # --- timezone sanity heuristic ---------------------------------------------
    if df["volume"].sum() > 0 and res in ("1m", "1s"):
        by_hour = (
            df.with_columns(pl.col("timestamp_ny").dt.hour().alias("_h"))
            .group_by("_h")
            .agg(pl.col("volume").sum().alias("v"))
            .sort("v", descending=True)
        )
        peak_hour = int(by_hour["_h"][0])
        if not (8 <= peak_hour <= 16):
            report.add(
                "timezone_suspicious", "WARNING",
                f"volume peaks at {peak_hour}:00 New York — expected the cash "
                "session; check that source timestamps are really UTC",
            )

    return report
