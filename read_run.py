"""Read a stored cycle/attempt report; unfinished records are reported incomplete."""

import argparse
import json
import sys

from sqlalchemy.exc import SQLAlchemyError

from database import make_engine
from run_records import read_run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=int)
    args = parser.parse_args()
    if args.run_id < 1:
        parser.error("run_id must be positive")
    engine = None
    try:
        engine = make_engine()
        result = read_run(engine, args.run_id)
        if result is None:
            print(f"No run with ID {args.run_id}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    except (SQLAlchemyError, ValueError) as error:
        print(f"Run read failed ({type(error).__name__})", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
