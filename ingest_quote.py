"""Step 2B: fetch one live quote, commit it, and print a database read-back."""

import argparse
from datetime import datetime, timezone
import json
import logging
import sys

import httpx
from sqlalchemy.exc import SQLAlchemyError

from database import make_engine
from fetch_quote import fetch_live_quote
from fetch_raw import BASE_URL, TIMEOUT
from persistence import minute_slot, read_observation, save_observation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", help="Exact Kalshi binary-market ticker")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    engine = None
    snapshot_id = None
    try:
        slot = minute_slot(datetime.now(timezone.utc))
        with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT) as client:
            market, quote = fetch_live_quote(client, args.ticker)
        if quote.is_crossed:
            logging.warning("%s: crossed quote; preserving observed values", args.ticker)
        engine = make_engine()
        saved = save_observation(engine, market, quote, slot)
        snapshot_id = saved["snapshot_id"]
        print(f"Run {saved['run_id']}: {saved['outcome']}; snapshot {snapshot_id}", file=sys.stderr)
        result = read_observation(engine, snapshot_id)
        if result is None:
            raise ValueError("Committed snapshot was not found during read-back")
        result["save_outcome"] = saved["outcome"]
        result["attempt_run_id"] = saved["run_id"]
        print("STORED observation (read from PostgreSQL):")
        print(json.dumps(result, indent=2))
    except (httpx.HTTPError, SQLAlchemyError, ValueError) as error:
        if snapshot_id is not None:
            logging.error("Snapshot %s is saved, but read-back failed: %s", snapshot_id, error)
        else:
            logging.error("Collection failed: %s", error)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
