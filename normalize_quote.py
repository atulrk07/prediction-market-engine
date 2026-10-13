"""Step 1B: validate raw Kalshi bids and calculate an exact YES quote."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


PRICE_DECIMAL_PLACES = 4
QUANTITY_DECIMAL_PLACES = 2


class Quote(BaseModel):
    """Normalized values; Decimal fields serialize to JSON strings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    ticker: str = Field(min_length=1)
    observed_at: AwareDatetime
    source_timestamp: AwareDatetime | None = None
    yes_bid: Decimal | None = Field(ge=0, le=1, decimal_places=4, allow_inf_nan=False)
    yes_bid_quantity: Decimal | None = Field(ge=0, decimal_places=2, allow_inf_nan=False)
    yes_ask: Decimal | None = Field(ge=0, le=1, decimal_places=4, allow_inf_nan=False)
    yes_ask_quantity: Decimal | None = Field(ge=0, decimal_places=2, allow_inf_nan=False)
    spread: Decimal | None = Field(ge=-1, le=1, decimal_places=4, allow_inf_nan=False)
    is_crossed: bool


def parse_decimal(value: object, label: str, decimal_places: int) -> Decimal:
    """Require finite fixed-point strings and reject precision without rounding."""
    if not isinstance(value, str):
        raise ValueError(f"{label}: expected a fixed-point string")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label}: invalid decimal string") from error
    if not number.is_finite():
        raise ValueError(f"{label}: must be finite")
    if number < 0:
        raise ValueError(f"{label}: must be nonnegative")
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
        raise ValueError(f"{label}: expected unsigned fixed-point notation")
    fraction = value.partition(".")[2]
    if len(fraction) > decimal_places:
        raise ValueError(f"{label}: at most {decimal_places} decimal places supported")
    return number


def best_bid(levels: object, side: str) -> tuple[Decimal | None, Decimal | None]:
    """Validate every level, ignore valid zero quantities, and find the maximum."""
    if not isinstance(levels, list):
        raise ValueError(f"{side}: expected an array")
    best_price = None
    best_quantity = None
    for index, level in enumerate(levels):
        label = f"{side}[{index}]"
        if not isinstance(level, list) or len(level) != 2:
            raise ValueError(f"{label}: expected [price_string, quantity_string]")
        price = parse_decimal(level[0], f"{label}.price", PRICE_DECIMAL_PLACES)
        quantity = parse_decimal(level[1], f"{label}.quantity", QUANTITY_DECIMAL_PLACES)
        if price > 1:
            raise ValueError(f"{label}.price: must be between 0 and 1")
        if quantity == 0:
            continue
        if best_price is None or price > best_price:
            best_price = price
            best_quantity = quantity
    return best_price, best_quantity


def normalize_orderbook(data: object, ticker: str, observed_at: datetime) -> Quote:
    """Normalize a complete book; a missing required side is never an empty side."""
    if not isinstance(data, dict) or not isinstance(data.get("orderbook_fp"), dict):
        raise ValueError("Expected an orderbook_fp object")
    book = data["orderbook_fp"]
    for side in ("yes_dollars", "no_dollars"):
        if side not in book:
            raise ValueError(f"orderbook_fp: missing required {side}")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")

    yes_bid, yes_quantity = best_bid(book["yes_dollars"], "yes_dollars")
    no_bid, no_quantity = best_bid(book["no_dollars"], "no_dollars")
    yes_ask = Decimal("1") - no_bid if no_bid is not None else None
    spread = yes_ask - yes_bid if yes_ask is not None and yes_bid is not None else None

    return Quote(
        ticker=ticker,
        observed_at=observed_at.astimezone(timezone.utc),
        # This endpoint supplies no documented book event timestamp.
        source_timestamp=None,
        yes_bid=yes_bid,
        yes_bid_quantity=yes_quantity,
        yes_ask=yes_ask,
        yes_ask_quantity=no_quantity,
        spread=spread,
        is_crossed=spread is not None and spread < 0,
    )
