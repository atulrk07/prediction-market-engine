"""Step 3A: read-only health, readiness, and stored-market HTTP routes."""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from database import make_engine
from market_queries import find_market, list_markets
from models import markets


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Creating an engine does not connect yet, so liveness works during an outage.
    app.state.engine = make_engine()
    try:
        yield
    finally:
        app.state.engine.dispose()


app = FastAPI(
    title="Prediction Market Data API",
    description="Read stored Kalshi market metadata from PostgreSQL.",
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


@app.get("/health", response_model=HealthResponse)
def health():
    """Liveness: this API process can respond, independently of the database."""
    return {"status": "ok"}


@app.get("/ready", response_model=ReadyResponse, responses=DATABASE_ERRORS)
def ready(connection: DatabaseConnection):
    """Readiness: the database and required market table can be read."""
    connection.execute(select(markets.c.id).limit(1)).first()
    return {"status": "ready"}


@app.get("/markets", response_model=list[MarketResponse], responses=DATABASE_ERRORS)
def market_list(
    connection: DatabaseConnection,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
):
    """Stored markets, including closed markets, ordered by database ID."""
    return list_markets(connection, limit, offset)


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
