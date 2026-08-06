# First Presented FVG — NQ / MNQ research backtester

A local backtesting and strategy-research application for the ICT-style
**First Presented Fair Value Gap** strategy on Nasdaq-100 futures (**NQ** and
**MNQ**).

> **Historical research only.** This application reads past market data and
> writes files. It has no broker integration and never places an order.

It answers the questions the strategy actually raises: which conditions
around the first significant one-minute FVG after the 9:30 New York open
produce the best expectancy, which produce clean delivery into liquidity,
which produce profitable-but-stressful trades, which signal a coming range,
when the original direction beats the inversion, and whether any of it holds
up across contracts, instruments and out-of-sample periods.

---

## Quick start

**Windows PowerShell**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env
python scripts\make_sample_data.py
pytest
streamlit run app.py
```

**macOS / Linux**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
python scripts/make_sample_data.py
pytest
streamlit run app.py
```

Python 3.12 is required. `scripts/make_sample_data.py` writes the offline
sample candles under `sample_data/`, so the dashboard and CLI work before you
have any Databento credentials.

### First run, no credentials needed

```bash
fvg-backtest run --symbol NQ \
  --provider parquet --path sample_data/NQ_1m_2025Q1.parquet \
  --start 2025-01-06 --end 2025-03-28
```

That prints a summary and writes `runs/<run_id>/` with the full result set.
Open the dashboard and load the run from the **Backtest** page.

### With Databento

Put your key in `.env`:

```
DATABENTO_API_KEY=db-your-key-here
```

Then:

```bash
fvg-backtest credentials                       # checks the key, never prints it
fvg-backtest download --provider databento --symbol NQ \
  --start 2025-01-01 --end 2025-12-31 --resolution 1m
fvg-backtest run --symbol NQ --config config/nq.yaml \
  --start 2025-01-01 --end 2025-12-31
```

---

## The strategy, as implemented

### Finding the zone

Scanning starts at **09:30 New York**. By default all three candles of a
gap must begin at or after the open, so the earliest possible FVG is
9:30 / 9:31 / 9:32 (`fvg.all_candles_after_open: false` relaxes this to
"only candle 3 must complete after the open").

A bullish FVG exists when `candle_3.low > candle_1.high`; a bearish one when
`candle_3.high < candle_1.low`. Candidates are scanned chronologically and
the **first** one that is significant becomes the session's reference zone.
Later gaps never replace it — but every candidate, including the rejects and
their rejection reason, is written to `setups.parquet`.

Significance is two independent tests, and a candidate needs only one:

**Type A — the gap is big and was not almost closed by the wicks.**

```
normalized_gap     = gap_width / atr                      >= 0.10
body_void          = candle_3.body_bottom - candle_1.body_top   (bullish)
preservation_ratio = gap_width / body_void                >= 0.50
```

**Type B — a recent wick reached into the gap.** Every one of the 15
completed candles before candle 1 is measured; a wick qualifies when it is
long in ATR terms (≥ 0.15), a large share of its candle (≥ 0.40), *and*
overlaps the gap by ≥ 25% of the gap's width.

Setups are labelled `A_ONLY`, `B_ONLY` or `A_AND_B`, and every raw
measurement behind the label is stored — nothing is reduced to a boolean.

### Living with the zone

Mitigation does not invalidate the zone: touches, penetration depth, closes
inside, midpoint touches and time spent inside are all recorded and the zone
stays active for the session. A wick straight through the zone changes
nothing. **Only a completed candle closing beyond the opposite boundary
inverts it**, and the same zone can invert repeatedly:

```
ORIGINAL_BULLISH --close < fvg_low--> BEARISH_INVERSION --close > fvg_high--> BULLISH_REINVERSION …
```

### Entries

| | entry | stop | target |
|---|---|---|---|
| bullish original | `fvg_high` (proximal) | `candle_1.low` | nearest untaken swing high |
| bearish original | `fvg_low` (proximal) | `candle_1.high` | nearest untaken swing low |
| inversion | opposite edge | selected stop model + buffer | nearest untaken level |

An order arms on the **bar after** the candle that created it — candle 3
cannot fill the order it established, and an inversion cannot fill on its own
confirmation candle. Entry models (`PROXIMAL_EDGE`, `MIDPOINT`,
`DISTAL_EDGE`) and the four inversion stop models are configurable, and
results from different inversion stop models are always reported separately,
never pooled.

Targets are confirmed swing points (1×1, 2×2 or 3×3 pivots, each usable only
after its right shoulder closes) from the trailing 60 minutes, at least 5
minutes old, not yet swept, on the far side of the entry. The **closest by
price** wins — never simply the newest. When nothing qualifies the setup is
labelled `NO_TARGET`; a target is never invented.

Pending orders are cancelled when the target is swept, the zone inverts
against them, the session ends, they exceed their maximum age, the contract
changes, or the data has a hole.

### Execution

`ONE_MINUTE_CONSERVATIVE` (the default) assumes the adverse order whenever a
single minute touches several levels — entry then stop, stop before target —
and logs every ambiguity as an `AMBIGUOUS_SEQUENCE` event. The favourable
extreme is not credited on a bar that resolves adversely, so MAE/MFE can
never describe a path the simulator did not assume.

`ONE_SECOND_INTRABAR` and `TICK_INTRABAR` replay finer data to establish the
real order, and only fall back to the conservative rule when a single second
or tick spans both levels.

Gross and net are always reported separately. Costs are per instrument:
commission, exchange/clearing fees, spread, and separate entry/stop/target
slippage in ticks. Adverse fills round *away* from the trade, so a sub-tick
cost is never rounded back to free.

---

## Command line

```bash
fvg-backtest contracts --root NQ                        # metadata and expirations
fvg-backtest credentials                                # Databento key check
fvg-backtest download --provider databento --symbol NQ \
    --start 2025-01-01 --end 2025-12-31 --resolution 1m
fvg-backtest download --provider databento --symbol NQ \
    --start 2025-01-01 --end 2025-12-31 --resolution 1s
fvg-backtest run --symbol NQ --config config/nq.yaml
fvg-backtest run --contract NQH25 --provider parquet \
    --path sample_data/NQ_1m_2025Q1.parquet \
    --start 2025-01-06 --end 2025-03-14
fvg-backtest compare --symbols NQ MNQ --start 2025-01-01 --end 2025-12-31
fvg-backtest report --run-id <RUN_ID> --by significance_type
fvg-backtest walkforward --symbol NQ --config config/nq.yaml
fvg-backtest cache
```

PowerShell uses a backtick for line continuation:

```powershell
fvg-backtest run `
  --symbol NQ `
  --config config/nq.yaml
```

---

## Dashboard

`streamlit run app.py` opens nine pages:

| page | what it is for |
|---|---|
| **Data** | instrument, dated contract or continuous mode, date range, resolution, credential status, download / import, cache inventory, data-quality report, roll calendar |
| **Strategy settings** | every rule and threshold, with YAML import and export |
| **Backtest** | sessions processed, candidates, rejects, qualifying FVGs, type classification, orders, fills, original / inversion / re-inversion counts, ambiguous fills, the event log, data warnings |
| **Results** | equity curve, cumulative R, net dollars, drawdown, monthly results, expectancy, profit factor, clean/sweaty/range rates, NQ vs MNQ, original vs inversion, contract consistency |
| **Conditions explorer** | any feature, categorical or binned by quantile or custom edges, against expectancy, MAE, MFE, sweaty rate and range rate |
| **Trade explorer** | the annotated session chart — zone, type, qualifying wick, midpoint, entry, stop, target, pivots, overnight and opening levels, mitigations, inversions — plus why the FVG qualified |
| **Range analysis** | probability of ranging by entry delay, efficiency, overlap, gap size, target age and distance, opening/overnight position, inversion count, time of day |
| **Walk-forward** | development / validation / out-of-sample folds and how far performance degrades |
| **Predictive models** | optional probabilities with calibration, ROC-AUC, precision, recall, Brier score, importance and partial dependence |

---

## Data

**Databento** is the primary provider (`GLBX.MDP3`). Symbol mapping is
automatic: dated contracts resolve to raw symbols (`NQH25` → `NQH5`), and
continuous requests use the **parent** symbol so every contract comes back
separately with its own `underlying_contract`. Requests are chunked, rate
limits are retried with exponential backoff, and results are cached as
Parquet under `data_cache/`.

**CSV and Parquet** work offline. Column names are matched
case-insensitively with common aliases, timestamps may be ISO strings or
epochs, and a contract code in the file name (`NQH25_1m.csv`) is picked up
automatically.

Every provider normalizes to one schema — `timestamp_utc`, `timestamp_ny`,
`trading_date`, `globex_session_date`, `symbol`, `root_symbol`,
`underlying_contract`, OHLC, `volume`, `trade_count`, `vwap`, `source`,
`resolution` — and only the timestamp and OHLC are mandatory. Validation
checks duplicates, missing bars, ordering, OHLC relationships, mid-session
contract changes, daylight-saving transitions, holidays, early closes, long
gaps, zero-volume bars and timezone plausibility.

### Contracts and rolls

**Mode A — individual contract** (`--contract NQH25`) is the preferred mode
for execution testing: one real contract, real prices, nothing stitched.

**Mode B — continuous research series** builds a series from an explicit
roll rule: `HIGHEST_VOLUME`, `FIXED_DAYS_BEFORE_EXPIRATION`, or
`USER_DEFINED_ROLL_CALENDAR`. Every candle keeps its
`underlying_contract`, `roll_method` and `days_to_expiration`, and each
session comes wholly from one contract — contracts are never blended.

Volume-based rolls use the previous session's completed volume, so the front
contract is never chosen with information the day had not yet produced.

**Prices are not back-adjusted by default.** The strategy trades actual
levels — gap edges, swing highs and lows, stop distances — and an additive
roll offset corrupts all of them. `rolls.back_adjust: true` exists and warns
loudly when used.

Sessions are classified `NORMAL`, `ROLLOVER_TRANSITION` or
`EXPIRATION_WEEK` so the three can be compared, and either of the latter two
can be excluded. No trade is ever carried across a contract change.

---

## Outputs

Every run gets a unique ID and a directory:

```
runs/{run_id}/config.yaml          the exact resolved configuration
runs/{run_id}/contracts.parquet    which contract each session came from
runs/{run_id}/setups.parquet       every candidate, including rejects and reasons
runs/{run_id}/trades.parquet       one row per filled trade
runs/{run_id}/events.parquet       the full event log
runs/{run_id}/daily_results.parquet
runs/{run_id}/summary.json
runs/{run_id}/report.html
```

Events cover candidate detection and rejection, FVG selection, mitigation,
entry activation, fill, cancellation, inversion, re-inversion, stop, target,
session close, contract roll and range onset. Every table exports to CSV
from the dashboard.

---

## Trade quality

Each trade carries its path metrics (gross/net points, ticks and dollars, R,
MAE and MFE in four units, time to 0.25R / 0.5R / 1R / target, entry and
midpoint crossings, mitigations before and after entry, direction changes)
and a label — plus **every underlying condition**, so the label definitions
themselves can be questioned:

- **Clean win** — target first, MAE ≤ 0.35R, ≤ 1 entry recross, 0.5R within
  5 minutes, done inside 15 minutes.
- **Sweaty win** — a win meeting at least two stress conditions (deep MAE,
  repeated recrossings, slow to 0.5R, long duration, gave back to near the
  stop, high post-entry overlap).
- **Immediate failure** — stopped before MFE reached 0.25R.
- **Stalled** — ten minutes in with no resolution and MFE below 0.5R.
- **Ranging** — at least two of: repeated entry and midpoint crossings, low
  efficiency ratio, little net progress, high overlap, no resolution, zone
  direction flipping repeatedly.

Retrospective **range onset** is computed separately and is explicitly a
research label: it looks into the future by design and is structurally
excluded from every model's feature set.

---

## Validation

Walk-forward is chronological. Thresholds are optimized **only** inside the
development window, then frozen for validation and out-of-sample. Random
splitting is not offered as the primary method because it leaks the future
into the past. Folds that only work in their development window, and filters
that only work in one contract or one short period, are flagged rather than
averaged away.

Small samples are never ranked as reliable: every statistics block carries
`reliable` and a warning, and the tables sort unreliable groups last so a
three-trade group cannot top a leaderboard.

---

## Configuration

`config/default.yaml` holds every knob with its default. `config/nq.yaml`
and `config/mnq.yaml` are thin overlays; later files win, and nested keys
merge, so an overlay only needs the keys it changes:

```bash
fvg-backtest run --symbol NQ --config config/nq.yaml
```

Instrument metadata — tick size, point value, tick value, exchange, contract
months, expiration rule, symbol mapping, and per-instrument costs — lives in
configuration, never in strategy logic. The dashboard's **Strategy
settings** page edits all of it and exports the result as YAML.

---

## Project layout

```
app.py                              Streamlit entry point
config/                             default.yaml, nq.yaml, mnq.yaml
sample_data/                        offline candles (regenerate with scripts/)
scripts/make_sample_data.py
tradingview/nq_first_presented_fvg.pine
src/fvg_backtest/
    config/        pydantic schema + YAML loading
    data/          provider interface, Databento, CSV/Parquet, cache,
                   normalization, quality validation, synthetic scenarios
    futures/       contract codes, expirations, roll schedules, continuous series
    sessions/      trading calendar and the New York session clock
    fvg/           detection, Type A/B significance, persistent zone state machine
    liquidity/     pivots without lookahead, sweeps, target selection, context levels
    execution/     costs, intrabar sequencing, orders, the session simulator
    features/      indicators and entry-time feature assembly
    analytics/     trade metrics, labels, conditional statistics, walk-forward, models
    visualization/ Plotly figures
    dashboard/     Streamlit pages
    cli/           the fvg-backtest command
tests/
```

---

## TradingView companion

`tradingview/nq_first_presented_fvg.pine` (Pine Script v6) draws the same
setup on a 1-minute NQ or MNQ chart: the zone, its type, the qualifying
prior wick, proximal/midpoint/distal boundaries, every inversion, the
proposed entry, stop and target with target age and R distance, the opening
ranges and the overnight high and low. It evaluates on confirmed candles
only, so it does not repaint or read ahead, and it exposes the same
thresholds as inputs.

**The Python application is the research source of truth.** The Pine script
is for visual validation.

---

## Testing

```bash
pytest                      # the whole suite
pytest tests/test_fvg_detection.py -v
```

Tests are deterministic. Rule-level tests use hand-built candles where every
expected number is worked out by hand; end-to-end tests run planted
scenarios — clean bullish, clean bearish, inversion, ranging, sweaty win and
a session with no FVG at all — whose correct outcome is known by
construction. The synthetic generator gives non-planted candles wicks wide
enough that an accidental FVG is mathematically impossible, so "first
presented" always means the planted pattern.

---

## Ambiguity policy

Where the strategy rules leave room for interpretation, this implementation
takes the most conservative backtesting assumption, makes it configurable,
documents it here, and records when it changes an outcome:

| ambiguity | assumption | switch |
|---|---|---|
| entry and stop in one candle | entry, then stop | `execution.mode` |
| stop and target in one candle after entry | stop first | `execution.mode` |
| favourable excursion on a bar that resolves adversely | not credited | — (logged as `AMBIGUOUS_SEQUENCE`) |
| close exactly on a zone boundary | does not invert | `zone.invert_on_touch_close` |
| price exactly touching a swing level | not a sweep | `liquidity.count_exact_touch_as_sweep` |
| candle 3 of the FVG | cannot fill its own order | — |
| the inversion confirmation candle | cannot fill the inversion order | — |
| a sub-tick cost | rounds against the trade | — |
| continuous-series prices | not back-adjusted | `rolls.back_adjust` |
| no eligible target | `NO_TARGET`, no trade | `targets.*` |
| missing bars while an order is working | cancel, `DATA_INCOMPLETE` | — |
