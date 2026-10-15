"""Replay a saved observation with its original slot and no exchange requests."""

import argparse
from datetime import datetime
import json
import logging
import sys

from sqlalchemy.exc import SQLAlchemyError

from database import make_engine
from normalize_quote import Quote
from persistence import read_observation, save_observation


def replay_observation(engine, snapshot_id: int) -> dict:
    original = read_observation(engine, snapshot_id)
    if original is None:
        raise ValueError(f"No stored observation with snapshot ID {snapshot_id}")
    # The saved quote's JSON strings become exact Decimal/time values again.
    quote = Quote.model_validate_json(json.dumps(original["quote"]))
    market = {
        "ticker": quote.ticker, "title": original["title"],
        "status": original["market_status"], "market_type": "binary",
        "close_time": original["close_time"],
    }
    slot = datetime.fromisoformat(original["collection_slot"])
    saved = save_observation(engine, market, quote, slot)
    return {
        **saved,
        "observation": read_observation(engine, saved["snapshot_id"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_id", type=int, help="Existing snapshot to replay")
    args = parser.parse_args()
    if args.snapshot_id < 1:
        parser.error("snapshot_id must be positive")
    engine = None
    try:
        engine = make_engine()
        result = replay_observation(engine, args.snapshot_id)
        print("REPLAY result (stored data; no new exchange request):")
        print(result["outcome"])
    except (SQLAlchemyError, ValueError) as error:
        logging.error("Replay failed: %s", error)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
