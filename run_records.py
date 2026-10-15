"""Persist cycle outcomes; reconstruct inserted counts from committed snapshots."""

from sqlalchemy import func, select

from models import ingestion_runs, snapshots


FAILURES = {"fetch_failed", "validation_failed", "storage_failed"}


def start_run(engine, slot, tickers, config) -> int:
    outcomes = {ticker: {"outcome": "not_attempted", "attempted": False, "reason": "pending"} for ticker in tickers}
    with engine.begin() as connection:
        return connection.execute(ingestion_runs.insert().values(
            collection_slot=slot, watchlist_config={"watchlist": tickers, **config}, market_outcomes=outcomes,
        ).returning(ingestion_runs.c.id)).scalar_one()


def put_outcome(connection, run_id: int, ticker: str, outcome: dict) -> dict:
    row = connection.execute(select(ingestion_runs).where(ingestion_runs.c.id == run_id).with_for_update()).mappings().one()
    if row["finished_at"] is not None or row["status"] != "running":
        raise ValueError("Cannot change a finished run")
    if ticker not in row["watchlist_config"]["watchlist"]:
        raise ValueError("Ticker does not belong to this run")
    outcomes = dict(row["market_outcomes"])
    previous = outcomes.get(ticker, {})
    # A lost commit acknowledgement must not overwrite a committed success with a failure.
    if previous.get("outcome") in {"inserted", "duplicate"} and outcome["outcome"] in FAILURES:
        return previous
    outcomes[ticker] = outcome
    attempted = sum(bool(value.get("attempted")) for value in outcomes.values())
    connection.execute(ingestion_runs.update().where(ingestion_runs.c.id == run_id).values(
        market_outcomes=outcomes, attempted_count=attempted,
    ))
    return outcome


def record_outcome(engine, run_id: int, ticker: str, outcome: dict) -> dict:
    with engine.begin() as connection:
        return put_outcome(connection, run_id, ticker, outcome)


def finish_run(engine, run_id: int, *, interrupted: bool = False, unvisited_reason: str = "cycle_budget_exhausted") -> dict:
    with engine.begin() as connection:
        row = connection.execute(select(ingestion_runs).where(ingestion_runs.c.id == run_id).with_for_update()).mappings().one()
        if row["finished_at"] is not None:
            raise ValueError("Run is already finished")
        outcomes = {ticker: ({**item, "reason": unvisited_reason} if item.get("reason") == "pending" else item)
                    for ticker, item in row["market_outcomes"].items()}
        failed = sum(item["outcome"] in FAILURES for item in outcomes.values())
        skipped = sum(item["outcome"] == "not_attempted" and item.get("reason") != "market_not_active" for item in outcomes.values())
        successful = any(item["outcome"] in {"inserted", "duplicate"} or item.get("reason") == "market_not_active" for item in outcomes.values())
        unknown = any(item["outcome"] == "unknown" for item in outcomes.values())
        if interrupted or unknown:
            status = "incomplete"
        elif failed or skipped:
            status = "partial" if successful else "failed"
        else:
            status = "success"
        inserted = connection.execute(select(func.count()).select_from(snapshots).where(snapshots.c.ingestion_run_id == run_id)).scalar_one()
        duplicates = sum(item["outcome"] == "duplicate" for item in outcomes.values())
        summary = None if status == "success" else f"failed={failed}; not_attempted={skipped}; unknown={unknown}; interrupted={interrupted}"
        connection.execute(ingestion_runs.update().where(ingestion_runs.c.id == run_id).values(
            status=status, finished_at=func.now(), inserted_count=inserted, duplicate_count=duplicates,
            failed_count=failed, error_summary=summary, market_outcomes=outcomes,
        ))
    inactive = sum(item.get("reason") == "market_not_active" for item in outcomes.values())
    return {"run_id": run_id, "status": status, "attempted": row["attempted_count"], "inserted": inserted,
            "duplicate": duplicates, "failed": failed, "not_attempted": skipped, "not_active": inactive}


def read_run(engine, run_id: int) -> dict | None:
    with engine.connect() as connection:
        row = connection.execute(select(ingestion_runs).where(ingestion_runs.c.id == run_id)).mappings().one_or_none()
        if row is None:
            return None
        inserted = connection.execute(select(func.count()).select_from(snapshots).where(snapshots.c.ingestion_run_id == run_id)).scalar_one()
    result = dict(row)
    result["stored_status"] = result["status"]
    result["status"] = "incomplete" if result["finished_at"] is None else result["status"]
    result["inserted_count"] = inserted
    for field in ("collection_slot", "started_at", "finished_at"):
        result[field] = result[field].isoformat() if result[field] is not None else None
    return result
