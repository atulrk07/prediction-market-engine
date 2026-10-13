"""Offline behavior checks using explicitly synthetic books."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from normalize_quote import normalize_orderbook


BOOKS = json.loads((Path(__file__).parent / "fixtures" / "orderbooks.json").read_text())
OBSERVED_AT = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def normalized(case):
    return normalize_orderbook(BOOKS[case], "FIXTURE-EXAMPLE", OBSERVED_AT)


@pytest.mark.parametrize("case", ["two_sided", "unordered", "zero_quantity"])
def test_best_prices_and_corresponding_quantities(case):
    result = normalized(case)
    assert result.yes_bid == Decimal("0.4200")
    assert result.yes_bid_quantity == Decimal("10.00")
    assert result.yes_ask == Decimal("0.4500")
    assert result.yes_ask_quantity == Decimal("8.00")
    assert result.spread == Decimal("0.0300")
    assert not result.is_crossed


@pytest.mark.parametrize("case", ["yes_only", "no_only", "empty", "all_zero_quantity"])
def test_absent_sides_stay_null(case):
    result = normalized(case)
    expected_bid = Decimal("0.4200") if case == "yes_only" else None
    expected_ask = Decimal("0.4500") if case == "no_only" else None
    assert result.yes_bid == expected_bid
    assert result.yes_ask == expected_ask
    assert result.yes_bid_quantity == (Decimal("10.00") if case == "yes_only" else None)
    assert result.yes_ask_quantity == (Decimal("8.00") if case == "no_only" else None)
    assert result.spread is None
    assert not result.is_crossed
    payload = json.loads(result.model_dump_json())
    assert payload["yes_bid"] == ("0.4200" if case == "yes_only" else None)


def test_subpenny_prices_and_fractional_quantities_remain_exact():
    result = normalized("subpenny")
    assert result.yes_bid == Decimal("0.4201")
    assert result.yes_ask == Decimal("0.4498")
    assert result.spread == Decimal("0.0297")
    payload = json.loads(result.model_dump_json())
    assert payload["yes_bid"] == "0.4201"
    assert payload["yes_ask"] == "0.4498"
    assert payload["yes_bid_quantity"] == "10.25"
    assert payload["yes_ask_quantity"] == "8.75"


def test_crossed_quote_preserves_values():
    result = normalized("crossed")
    assert result.yes_bid == Decimal("0.4200")
    assert result.yes_ask == Decimal("0.4000")
    assert result.spread == Decimal("-0.0200")
    assert result.is_crossed


def test_zero_price_is_a_real_level():
    result = normalize_orderbook(
        {"orderbook_fp": {"yes_dollars": [["0", "1"]], "no_dollars": [["1", "2"]]}},
        "FIXTURE-ZERO", OBSERVED_AT,
    )
    assert result.yes_bid == Decimal("0")
    assert result.yes_ask == Decimal("0")
    assert result.spread == Decimal("0")
    assert result.yes_bid_quantity == Decimal("1")
    assert result.yes_ask_quantity == Decimal("2")


@pytest.mark.parametrize("side", ["yes_dollars", "no_dollars"])
@pytest.mark.parametrize("level,reason", [
    ([0.42, "1"], "fixed-point string"),
    (["0.42", 1], "fixed-point string"),
    (["NaN", "1"], "finite"),
    (["Infinity", "1"], "finite"),
    (["0.42", "NaN"], "finite"),
    (["0.42", "Infinity"], "finite"),
    (["-0.01", "1"], "nonnegative"),
    (["0.42", "-1"], "nonnegative"),
    (["1.0001", "1"], "between 0 and 1"),
    (["0.42001", "1"], "decimal places"),
    (["0.42000", "1"], "decimal places"),
    (["0.42", "1.001"], "decimal places"),
    (["4.2e-1", "1"], "fixed-point notation"),
    (["invalid", "1"], "invalid decimal"),
    (["0.42"], "expected"),
    (["0.42", "1", "extra"], "expected"),
    (None, "expected"),
    (["invalid", "0"], "invalid decimal"),
])
def test_invalid_level_rejects_whole_book(side, level, reason):
    book = {"yes_dollars": [["0.42", "10"]], "no_dollars": [["0.55", "8"]]}
    book[side].append(level)
    with pytest.raises(ValueError, match=reason):
        normalize_orderbook({"orderbook_fp": book}, "FIXTURE-INVALID", OBSERVED_AT)


@pytest.mark.parametrize("data,reason", [
    (None, "orderbook_fp"),
    ([], "orderbook_fp"),
    ({}, "orderbook_fp"),
    ({"orderbook_fp": []}, "orderbook_fp"),
    (BOOKS["missing_side"], "missing required no_dollars"),
    ({"orderbook_fp": {"no_dollars": []}}, "missing required yes_dollars"),
    ({"orderbook_fp": {"yes_dollars": None, "no_dollars": []}}, "array"),
    ({"orderbook_fp": {"yes_dollars": [], "no_dollars": {}}}, "array"),
    (BOOKS["malformed"], "invalid decimal"),
])
def test_invalid_shape_differs_from_empty_book(data, reason):
    with pytest.raises(ValueError, match=reason):
        normalize_orderbook(data, "FIXTURE-INVALID", OBSERVED_AT)


def test_collection_time_converts_to_utc_without_inventing_source_time():
    local_time = datetime(2026, 10, 11, 5, tzinfo=timezone(timedelta(hours=-7)))
    result = normalize_orderbook(BOOKS["two_sided"], "FIXTURE-TIME", local_time)
    assert result.observed_at == OBSERVED_AT
    assert result.observed_at.utcoffset() == timedelta(0)
    assert result.source_timestamp is None
    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_orderbook(BOOKS["two_sided"], "FIXTURE-TIME", local_time.replace(tzinfo=None))
