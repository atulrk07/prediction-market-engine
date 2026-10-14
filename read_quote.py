"""Step 2B: read an existing observation by snapshot ID without HTTP requests."""

import argparse
import json
import logging
import sys

from sqlalchemy.exc import SQLAlchemyError

from database import make_engine
from persistence import read_observation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_id", type=int, help="ID printed by ingest_quote.py")
    args = parser.parse_args()
    if args.snapshot_id < 1:
        parser.error("snapshot_id must be positive")
    engine = None
    try:
        engine = make_engine()
        result = read_observation(engine, args.snapshot_id)
        if result is None:
            print(f"No stored observation with snapshot ID {args.snapshot_id}", file=sys.stderr)
            return 1
        print("STORED observation (read from PostgreSQL):")
        print(json.dumps(result, indent=2))
    except (SQLAlchemyError, ValueError) as error:
        logging.error("Database read failed: %s", error)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
