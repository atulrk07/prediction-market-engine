"""Stored market reads and local/private alert-rule management."""

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path as FilePath
from typing import Annotated, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from database import make_engine
from history import history_range
from latest_quote import build_latest_quote, stale_after_seconds, utc_now
from market_queries import find_latest_snapshot, find_market, list_history, list_markets, market_has_observations
from models import alert_rules, markets, snapshots
from normalize_quote import Quote
from persistence import quote_from_snapshot


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadyResponse(BaseModel):
    status: Literal["ready"]


class ErrorResponse(BaseModel):
    detail: str


class MarketResponse(BaseModel):
    id: int
    platform: str
    external_id: str
    title: str
    status: str
    close_time: datetime | None
    updated_at: datetime


class AlertRuleCreate(BaseModel):
    market_id: int = Field(gt=0, le=9223372036854775807)
    threshold: Decimal = Field(gt=Decimal("0"), lt=Decimal("1"), allow_inf_nan=False)


class AlertRuleResponse(BaseModel):
    id: int
    market_id: int
    threshold: Decimal
    enabled: bool
    is_alerted: bool
    last_alerted_at: AwareDatetime | None


class LatestQuoteResponse(BaseModel):
    market_id: int
    ticker: str
    market_status: str = Field(description="Last stored exchange market status, independent of quote status.")
    status: Literal["no_observations", "empty_book", "one_sided", "two_sided", "crossed"]
    snapshot_id: int | None
    ingestion_run_id: int | None
    collection_slot: AwareDatetime | None
    age_seconds: float | None = Field(
        allow_inf_nan=False,
        description="Seconds since observed_at, not the exchange event time. Null without observations; negative signals clock skew.",
    )
    is_stale: bool | None = Field(
        description="True when age exceeds stale_after_seconds or is negative; null without observations.",
    )
    stale_after_seconds: int = Field(gt=0)
    quote: Quote | None = Field(description="Latest stored observation; Decimal prices and quantities are JSON strings.")


class HistoryObservation(BaseModel):
    snapshot_id: int
    ingestion_run_id: int
    collection_slot: AwareDatetime
    quote: Quote


class HistoryResponse(BaseModel):
    market_id: int
    ticker: str
    market_status: str = Field(description="Last stored market status, not the status at each historical observation.")
    status: Literal["ok", "no_observations"] = Field(
        description="no_observations means this market has no snapshots at all. An empty page can still have status ok.",
    )
    start: AwareDatetime = Field(description="Inclusive start of the selected UTC range.")
    end: AwareDatetime = Field(description="Exclusive end of the selected UTC range.")
    limit: int
    offset: int
    observations: list[HistoryObservation]


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.stale_after_seconds = stale_after_seconds()
    # Creating an engine does not connect yet, so liveness works during an outage.
    app.state.engine = make_engine()
    try:
        yield
    finally:
        app.state.engine.dispose()


app = FastAPI(
    title="Prediction Market Data API",
    description="Read stored Kalshi data and manage price alerts. GET requests do not refresh Kalshi. Alert writes are for local/private use; no authentication is provided.",
    version="0.0.1",
    lifespan=lifespan,
)


def get_connection(request: Request) -> Iterator[Connection]:
    try:
        with request.app.state.engine.connect() as connection:
            yield connection
    except SQLAlchemyError:
        # Keep database details and credentials out of HTTP responses.
        raise HTTPException(status_code=503, detail="Database unavailable") from None


DatabaseConnection = Annotated[Connection, Depends(get_connection)]
DATABASE_ERRORS = {503: {"model": ErrorResponse, "description": "Database unavailable"}}
DASHBOARD = FilePath(__file__).with_name("dashboard.html")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    """Static page that reads the JSON routes below from the browser."""
    return FileResponse(DASHBOARD, media_type="text/html")


@app.get("/health", response_model=HealthResponse)
def health():
    """Liveness: this API process can respond, independently of the database."""
    return {"status": "ok"}


@app.get("/ready", response_model=ReadyResponse, responses=DATABASE_ERRORS)
def ready(connection: DatabaseConnection):
    """Readiness: the database and both required read tables can be read."""
    connection.execute(select(markets.c.id).limit(1)).first()
    connection.execute(select(snapshots.c.id).limit(1)).first()
    return {"status": "ready"}


@app.get("/markets", response_model=list[MarketResponse], responses=DATABASE_ERRORS)
def market_list(
    connection: DatabaseConnection,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
):
    """Stored markets, including closed markets, ordered by database ID."""
    return list_markets(connection, limit, offset)


@app.post(
    "/alert-rules", response_model=AlertRuleResponse, status_code=201,
    responses={**DATABASE_ERRORS, 404: {"model": ErrorResponse, "description": "Unknown market"}},
)
def create_alert_rule(rule: AlertRuleCreate, connection: DatabaseConnection):
    """Local/private use: create an enabled rule; thresholds are exact decimals."""
    if find_market(connection, rule.market_id) is None:
        raise HTTPException(status_code=404, detail="Market not found")
    saved = connection.execute(alert_rules.insert().values(
        market_id=rule.market_id, threshold=rule.threshold,
    ).returning(alert_rules)).mappings().one()
    connection.commit()
    return saved


@app.get("/alert-rules", response_model=list[AlertRuleResponse], responses=DATABASE_ERRORS)
def list_alert_rules(
    connection: DatabaseConnection,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
):
    """Rules, including disabled rules, ordered by database ID."""
    return connection.execute(select(alert_rules).order_by(alert_rules.c.id).limit(limit).offset(offset)).mappings().all()


@app.post(
    "/alert-rules/{id}/disable", response_model=AlertRuleResponse,
    responses={**DATABASE_ERRORS, 404: {"model": ErrorResponse, "description": "Unknown alert rule"}},
)
def disable_alert_rule(
    connection: DatabaseConnection,
    id: Annotated[int, Path(gt=0, le=9223372036854775807)],
):
    """Local/private use: disable a rule; repeated requests are safe."""
    rule = connection.execute(alert_rules.update().where(alert_rules.c.id == id).values(
        enabled=False,
    ).returning(alert_rules)).mappings().one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    connection.commit()
    return rule


@app.get(
    "/markets/{id}", response_model=MarketResponse,
    responses={**DATABASE_ERRORS, 404: {"model": ErrorResponse, "description": "Unknown market"}},
)
def market_detail(
    connection: DatabaseConnection,
    id: Annotated[int, Path(gt=0, le=9223372036854775807)],
):
    """Return one stored market by its database ID, not its exchange ticker."""
    market = find_market(connection, id)
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")
    return market


@app.get(
    "/markets/{id}/quote", response_model=LatestQuoteResponse,
    responses={**DATABASE_ERRORS, 404: {"model": ErrorResponse, "description": "Unknown market"}},
)
def market_quote(
    request: Request,
    connection: DatabaseConnection,
    id: Annotated[int, Path(gt=0, le=9223372036854775807)],
):
    """Return the latest saved observation and its age, without exchange requests."""
    market = find_market(connection, id)
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")
    snapshot = find_latest_snapshot(connection, id)
    # Sample the clock after the reads, so a concurrent insert is not newer than now.
    return build_latest_quote(market, snapshot, utc_now(), request.app.state.stale_after_seconds)


@app.get(
    "/markets/{id}/history", response_model=HistoryResponse,
    responses={**DATABASE_ERRORS, 404: {"model": ErrorResponse, "description": "Unknown market"}},
)
def market_history(
    connection: DatabaseConnection,
    id: Annotated[int, Path(gt=0, le=9223372036854775807)],
    start: Annotated[AwareDatetime | None, Query(description="Inclusive timezone-aware start; defaults to 24 hours before end.")] = None,
    end: Annotated[AwareDatetime | None, Query(description="Exclusive timezone-aware end; defaults to now.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
):
    """Oldest first, including start and excluding end; at most 7 days and 1,000 rows per page."""
    try:
        start, end = history_range(start, end, utc_now())
    except (ValueError, OverflowError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    market = find_market(connection, id)
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")
    rows = list_history(connection, id, start, end, limit, offset)
    has_observations = bool(rows) or market_has_observations(connection, id)
    return {
        "market_id": id, "ticker": market["external_id"], "market_status": market["status"],
        "status": "ok" if has_observations else "no_observations",
        "start": start, "end": end, "limit": limit, "offset": offset,
        "observations": [
            {"snapshot_id": row["id"], "ingestion_run_id": row["ingestion_run_id"],
             "collection_slot": row["collection_slot"], "quote": quote_from_snapshot(row, market["external_id"])}
            for row in rows
        ],
    }
