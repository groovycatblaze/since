"""
api.py — HTTP surface.

Run:
    uvicorn app.api:app --reload

Authentication, honestly
------------------------
There is none worth the name. The caller identifies itself with an X-User
header and the server trusts it. This is NOT production auth and the README
says so plainly.

What DOES exist is the boundary: every query is scoped by user_id at the SQL
level, so one user's watermarks and watchlist can never leak into another's
digest. Sessions and password handling would slot in above this line without
touching anything below it. Claiming a real auth system here would be a worse
answer than naming the gap.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from app.config import FIXTURES_DIR, HISTORY_DIR, settings
from app.db import (
    acknowledge, add_item, ensure_user, ensure_watchlist, get_watchlist,
    health, init_pool, close_pool, remove_item, resolve_symbol,
    search_instruments,
)
from app.market_calendar import IST, TradingCalendar
from app.providers import LiveProvider, ReplayProvider
from app.service import MarketService

logging.basicConfig(level=settings.log_level)
log = logging.getLogger(__name__)

_service: MarketService | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build everything once at startup, not per request.

    ReplayProvider parses ~60,000 lines from disk; doing that per request
    would add hundreds of milliseconds to every call for no reason.
    """
    global _service
    init_pool()
    calendar = TradingCalendar.from_history(HISTORY_DIR)
    covered = calendar.covered_range
    log.info("trading calendar covers %s", covered)

    if settings.data_mode.upper() == "REPLAY":
        provider = ReplayProvider(FIXTURES_DIR)
        log.info("REPLAY mode: %d symbols, span %s",
                 len(provider.symbols), provider.span)
    else:
        provider = LiveProvider()
        log.info("LIVE mode: yfinance")

    _service = MarketService(provider, calendar)
    yield
    close_pool()


STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Since", version="1.0", lifespan=lifespan)

# The frontend is served by this same app, so every call is same-origin and
# CORS is not needed at all. Kept narrow rather than removed because a
# deployment that later splits the frontend onto its own host will want exactly
# this list -- and a wildcard on a financial API is a habit worth not forming
# even in a demo.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request models -- validation at the edge
# ---------------------------------------------------------------------------

class AddItem(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    threshold_price: float | None = Field(default=None, gt=0)

    @field_validator("symbol")
    @classmethod
    def clean(cls, v: str) -> str:
        return v.strip().upper()


class AckRequest(BaseModel):
    # Empty list means "everything currently shown", which is what the
    # "Mark all as seen" button sends.
    instrument_ids: list[int] = Field(default_factory=list)


def current_user(x_user: str = Header(default="demo@since.app")) -> tuple[int, int]:
    user_id = ensure_user(x_user)
    return user_id, ensure_watchlist(user_id)


def clock(at: str | None = Query(default=None)) -> datetime:
    """The demo clock.

    REPLAY mode accepts ?at=<ISO timestamp> so the returning-user scenario is
    reproducible on demand instead of depending on the market being open and
    interesting. LIVE mode ignores it -- otherwise the parameter would be a
    way to ask the system to lie about the present.
    """
    if at and settings.data_mode.upper() == "REPLAY":
        # "+05:30" arrives as " 05:30" when a caller forgets to URL-encode the
        # query string, because '+' IS the encoding for a space. Repair it
        # rather than rejecting: every hand-written curl and every browser
        # address bar hits this, and a 400 here looks like a broken clock
        # rather than a quoting mistake.
        repaired = re.sub(r"\s(\d{2}:\d{2})$", r"+\1", at.strip())
        try:
            parsed = datetime.fromisoformat(repaired)
        except ValueError:
            raise HTTPException(
                400, f"Invalid timestamp: {at!r}. Expected ISO-8601, "
                     f"e.g. 2026-09-04T15:24:00+05:30")
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    """The frontend is one static file served by the API.

    No bundler, no node_modules, no second dev server, no CORS in production.
    For a home screen that is one list with three sections, a build toolchain
    would be weight without a job -- and it means the whole product deploys as
    a single artifact.
    """
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def get_health():
    try:
        stats = health()
    except Exception as exc:                              # noqa: BLE001
        raise HTTPException(503, f"database unavailable: {exc}")
    return {"status": "ok", "mode": settings.data_mode, **stats}


@app.get("/api/instruments")
def get_instruments(q: str = Query(default="", max_length=32)):
    # 200, not the default 10: with an empty query this populates the whole
    # datalist, and a dropdown showing 10 of 33 instruments looks like the
    # other 23 do not exist.
    return {"results": search_instruments(q, limit=200)}


@app.get("/api/digest")
def get_digest(ctx=Depends(current_user), now: datetime = Depends(clock)):
    """The main read. Everything the home screen needs, in one call.

    One request rather than one-per-stock: a 20-stock watchlist making 20
    round trips is the usual reason these screens feel sluggish, and it also
    makes the tier counts inconsistent while they trickle in.
    """
    user_id, watchlist_id = ctx
    return _service.build_digest(user_id, watchlist_id, now)


@app.post("/api/watchlist", status_code=201)
def post_item(body: AddItem, ctx=Depends(current_user),
              now: datetime = Depends(clock)):
    user_id, watchlist_id = ctx
    found = resolve_symbol(body.symbol)
    if not found:
        raise HTTPException(404, f"Unknown symbol: {body.symbol}")
    item = add_item(watchlist_id, found["id"], body.threshold_price, now)
    return {"instrument_id": found["id"], "symbol": found["symbol"],
            "name": found["display_name"],
            "threshold_price": item.get("threshold_price")}


@app.delete("/api/watchlist/{instrument_id}", status_code=204)
def delete_item(instrument_id: int, ctx=Depends(current_user)):
    user_id, watchlist_id = ctx
    if not remove_item(watchlist_id, instrument_id, user_id):
        raise HTTPException(404, "Not in your watchlist")


@app.post("/api/acknowledge")
def post_acknowledge(body: AckRequest, ctx=Depends(current_user),
                     now: datetime = Depends(clock)):
    """Advance the user's watermark. THE core write.

    Only this endpoint moves a watermark. Opening the app, loading the digest
    and refreshing all leave it untouched -- otherwise a user who glances at
    the screen and gets distracted silently loses their diff, which is the bug
    that quietly breaks most implementations of "what changed since last time".

    Idempotent: the underlying UPDATE only fires when it would move the
    watermark FORWARD, so a retried or duplicated request is a no-op.
    """
    user_id, watchlist_id = ctx
    rows = get_watchlist(watchlist_id, user_id, now)

    wanted = set(body.instrument_ids)
    # acknowledged_at is WHEN THE USER ACKNOWLEDGED (the request clock), not
    # the exchange timestamp of the bar they happened to be looking at. Those
    # differ by up to a minute, and using the bar time would leave added_at
    # marginally ahead of acknowledged_at -- resurrecting the "always new" bug
    # in a form that is much harder to spot.
    entries = [
        (r["instrument_id"], now, float(r["price"]))
        for r in rows
        if r["price"] and (not wanted or r["instrument_id"] in wanted)
    ]

    updated = acknowledge(user_id, entries)
    return {"acknowledged": len(entries), "advanced": updated,
            "at": now.isoformat()}


@app.get("/api/demo/span")
def demo_span():
    """Bounds of the replay clock, so the UI can offer valid demo times
    instead of letting a user pick a moment with no data behind it."""
    if settings.data_mode.upper() != "REPLAY":
        return {"mode": "LIVE", "span": None}
    span = _service.provider.span
    return {"mode": "REPLAY",
            "span": [span[0].isoformat(), span[1].isoformat()] if span else None,
            "symbols": len(_service.provider.symbols)}
