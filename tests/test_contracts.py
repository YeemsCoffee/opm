from __future__ import annotations

from datetime import date

import pytest

from fvg_backtest.config import AppConfig
from fvg_backtest.futures import (
    contract_expiration,
    list_contracts,
    parse_contract,
    to_databento_raw,
)
from fvg_backtest.futures.rolls import (
    RollPeriod,
    build_roll_schedule,
    classify_roll_period,
    front_contract_for,
)


@pytest.fixture()
def nq(config: AppConfig):
    return config.instruments["NQ"]


def test_parse_canonical_codes():
    c = parse_contract("NQH25")
    assert (c.root, c.month, c.year) == ("NQ", 3, 2025)
    c = parse_contract("MNQZ24")
    assert (c.root, c.month, c.year) == ("MNQ", 12, 2024)
    assert c.code == "MNQZ24"


def test_parse_databento_single_digit_year():
    c = parse_contract("NQH5", reference=date(2025, 1, 15))
    assert c.year == 2025
    c = parse_contract("NQZ9", reference=date(2025, 1, 15))
    assert c.year == 2029
    # look back up to 2 years for recently expired contracts
    c = parse_contract("NQZ3", reference=date(2025, 1, 15))
    assert c.year == 2023


def test_databento_raw_mapping():
    assert to_databento_raw("NQH25", reference=date(2025, 1, 2)) == "NQH5"
    assert to_databento_raw("MNQM26", reference=date(2025, 1, 2)) == "MNQM6"


def test_third_friday_expirations(nq):
    assert contract_expiration(parse_contract("NQH25"), nq) == date(2025, 3, 21)
    assert contract_expiration(parse_contract("NQM25"), nq) == date(2025, 6, 20)
    assert contract_expiration(parse_contract("NQU25"), nq) == date(2025, 9, 19)
    assert contract_expiration(parse_contract("NQZ24"), nq) == date(2024, 12, 20)


def test_list_contracts_quarterlies_in_order(nq):
    cs = list_contracts(nq, date(2025, 1, 1), date(2025, 7, 1))
    codes = [c.code for c in cs]
    assert codes[:3] == ["NQZ24", "NQH25", "NQM25"]  # one prior + live ones
    assert "NQU25" in codes


def test_fixed_days_roll_schedule(nq, config):
    cfg = config.rolls.model_copy(update={"method": "FIXED_DAYS_BEFORE_EXPIRATION"})
    sched = build_roll_schedule(nq, date(2025, 1, 2), date(2025, 4, 30), cfg)
    # NQH25 expires 2025-03-21; roll 8 days earlier => last front day 03-13
    front_mar13 = front_contract_for(sched, date(2025, 3, 13))
    front_mar14 = front_contract_for(sched, date(2025, 3, 14))
    assert front_mar13.code == "NQH25"
    assert front_mar14.code == "NQM25"
    assert front_contract_for(sched, date(2025, 1, 2)).code == "NQH25"


def test_user_defined_roll_calendar(nq, config):
    cfg = config.rolls.model_copy(
        update={
            "method": "USER_DEFINED_ROLL_CALENDAR",
            "roll_calendar": {"NQH25": "2025-03-10"},
        }
    )
    sched = build_roll_schedule(nq, date(2025, 2, 1), date(2025, 4, 1), cfg)
    assert front_contract_for(sched, date(2025, 3, 10)).code == "NQH25"
    assert front_contract_for(sched, date(2025, 3, 11)).code == "NQM25"


def test_highest_volume_roll(nq, config):
    cfg = config.rolls.model_copy(update={"method": "HIGHEST_VOLUME"})
    vols = {
        "NQH25": {date(2025, 3, d): 500_000 - d * 10_000 for d in range(10, 21)},
        "NQM25": {date(2025, 3, d): 100_000 + d * 25_000 for d in range(10, 21)},
    }
    # NQM25 volume passes NQH25 when 100k+25k*d > 500k-10k*d  =>  d > 11.4
    sched = build_roll_schedule(nq, date(2025, 3, 1), date(2025, 3, 31), cfg, daily_volumes=vols)
    assert front_contract_for(sched, date(2025, 3, 12)).code == "NQH25"
    assert front_contract_for(sched, date(2025, 3, 13)).code == "NQM25"


def test_no_open_position_across_roll_is_flaggable(nq, config):
    period, dte = classify_roll_period(date(2025, 3, 17), "NQH25", nq, config.rolls)
    assert period == RollPeriod.EXPIRATION_WEEK
    assert dte == 4
    period, _ = classify_roll_period(date(2025, 3, 10), "NQH25", nq, config.rolls)
    assert period == RollPeriod.ROLLOVER_TRANSITION
    period, _ = classify_roll_period(date(2025, 2, 3), "NQH25", nq, config.rolls)
    assert period == RollPeriod.NORMAL
