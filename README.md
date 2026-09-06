# Since

**A watchlist with memory. It ranks by surprise, not by size.**

A 2% move in a stock that never moves is a bigger event than a 5% move in one
that always does. And if the whole market fell 2%, your stock falling 2% is not
news at all.

Since measures every change against two things most watchlists ignore: how
unusual that move is *for that particular stock*, and the exact moment *you*
last looked. When you come back after a day or a fortnight, it tells you what
actually happened while you were away — and, just as often, that nothing did.

---

## The problem

Open any watchlist and you get a grid of tickers with red and green percentages.
That answers "what is happening now". Nobody asked that. The question is:

> I last looked on Tuesday. What changed, which of it matters, and what should
> I look at first?

Three things make that hard:

1. **Percentages are not comparable across stocks.** SUZLON moving 2% is a
   normal Tuesday. HDFCBANK moving 2% is not. Ranking by percentage puts the
   volatile stocks permanently at the top, where they stop meaning anything.
2. **"Since you last checked" needs real memory.** Not since the market opened,
   not since midnight — since *you* looked. That state has to survive closing
   the tab and opening a different device.
3. **If everything is flagged, nothing is.** A watchlist that shouts every day
   trains you to stop reading it.

---

## What counts as a meaningful change

Four signals, deliberately few.

| Signal | What it measures | Shown as |
|---|---|---|
| **Normalised move** | Price change divided by that stock's own normal daily range, scaled for how long you were away | "3.2× its normal range" |
| **Volume surprise** | Volume so far today against volume normally expected *by this hour* | "Volume is 2.1× normal" |
| **Level crossed** | A price level you asked about, or a new high/low for the window | "Crossed your level of ₹1,200" |
| **Suspected corporate action** | A large move with no volume behind it, or a price ratio landing on a known split ratio | "May be a split or bonus — verify" |

These sort into three buckets — **Needs a look**, **Moved**, **Quiet** — and every
item carries the sentences explaining why it landed there. There is no composite
score. "Attention Score: 84.7" tells a user nothing they can act on and cannot be
defended when someone asks where 84.7 came from.

### The thresholds are measured, not guessed

`tools/calibrate.py` walks 90 days of recorded NSE history day by day. For each
day it builds a baseline from only the 30 days *before* it, scores that day, and
counts the result. Strictly walk-forward — no future data enters any baseline —
so these are the rates the live system would actually have produced:

```
Median |z| across 32 instruments   0.66      (a calibrated z-score sits near 0.674)
Needs a look                        5.1% of stock-days
Moved                              14.6%
Quiet                              80.2%
```

On a 20-stock watchlist that is roughly one item flagged and three non-quiet per
day. Few enough to still mean something.

Run it yourself: `python -m tools.calibrate`

### Two modelling decisions worth stating

**Volatility is estimated from the median absolute deviation, not the standard
deviation.** Measured on synthetic data: a single earnings-gap day inflates a
plain standard deviation by **64×** and the robust estimate by **1.4×**. With
plain stdev, one dramatic day inflates a stock's baseline for the following month
and silently suppresses every real signal in it afterwards — the app goes quiet on
a stock precisely when it starts mattering.

**There is no drift term.** The textbook z-score subtracts an expected return
(`mu × t`) before dividing. At this sample size that is wrong: 30 days is ample to
estimate volatility and nowhere near enough to estimate a mean return — the
standard error on `mu` exceeds `mu`. Including it put noise in the numerator, and
the visible symptom was `z` disagreeing in *sign* with the price move on screen
(+1.1% reading as z = −0.72). Volatility is estimable at short horizons; drift is
not.

**`z` is never presented as a probability.** Measured p95 |z| runs 2.4–4.7 across
instruments where a normal distribution gives 1.96, and one instrument reached
−12.3. Returns are fat-tailed, so "a 3-sigma event" would be a false claim. Every
message is relative — "3.2× its normal range" — which survives fat tails.

---

## Memory: how "since you last checked" works

One row per (user, instrument) holding the observation the user last
acknowledged.

**Viewing does not advance it.** Loading the page doesn't, refreshing doesn't,
moving the demo clock doesn't. Only pressing *Mark all as seen* does. This is the
bug that quietly breaks most implementations of this idea: a user glances at the
screen, gets distracted, closes the tab, and their diff is gone.

**Watermarks are monotonic.** The update only fires when it would move the
watermark *forward*:

```sql
ON CONFLICT (user_id, instrument_id) DO UPDATE
    SET acknowledged_at = EXCLUDED.acknowledged_at, ...
    WHERE user_watermarks.acknowledged_at < EXCLUDED.acknowledged_at
```

Two devices acknowledging at the same moment both succeed; the later timestamp
wins and the earlier becomes a no-op. No locks, no lost updates, and retries are
free because the operation is idempotent by construction.

**One clock authority.** `added_at` comes from the request clock, not the database
`NOW()`. Mixing an application clock with a database wall clock made every item
compare as "added after I last acknowledged" forever, and the product silently
degraded into a plain day-change tracker while every component looked correct.

**No lookahead.** The observation read is bounded by the request clock
(`exchange_time <= as_of`), so the newest bar *at or before* now is chosen, never
the newest bar in the table. The replay provider enforces the same invariant
independently with a binary search. In production the unbounded version breaks
under clock skew or a late-arriving backfill.

---

## Data freshness

Freshness is a typed state, not a timestamp the UI is left to interpret.

| State | When | Used for scoring? |
|---|---|---|
| `LIVE` | Market open, fetched under 60s ago | Yes |
| `DELAYED` | Market open, fetched 1–5 min ago | Yes |
| `CLOSED` | Outside trading hours | **Yes — previous close is authoritative** |
| `STALE` | Market open, data over 5 min old | **No** |
| `UNAVAILABLE` | Fetch failing past the retry budget | No |

**The rule: stale data may be displayed, but may never raise a new flag.** Showing
a price with a timestamp is honest. Telling someone a stock needs attention based
on a five-minute-old quote is not.

**`CLOSED` is not `STALE`.** At 8pm on a Saturday, Friday's close is 50 hours old
and simultaneously the most current price that exists. A flat age threshold labels
it broken and teaches users to distrust a correct number.

### The trading calendar is observed, not hardcoded

A hardcoded NSE holiday list rots annually and produces silently wrong answers
when stale. Instead the calendar is derived from which dates the exchange actually
published bars for: a date with a bar was a trading day, a date without one was
not. It self-corrects whenever history is re-recorded.

This matters for scoring, not just display. Friday close to Monday open is three
calendar days and roughly zero trading days — scaling volatility by √3 instead of
√0 understates every surprise by about 70%.

---

## Architecture

```
                    ┌───────────────────────────────┐
   browser  ────────▶  FastAPI  (modular monolith)  │
   one static          │                            │
   HTML file           │  routes ─▶ service ─▶ scoring (pure)
                       │              │        no I/O, 72 tests
                       │              ▼
                       │        provider (protocol)
                       │         ├── ReplayProvider  (recorded NSE fixtures)
                       │         └── LiveProvider    (yfinance, batched)
                       └──────────────┬────────────────┘
                                      ▼
                          PostgreSQL
                          shared market state
                          + personal watermarks
```

**Market state is shared; memory is personal.** This one sentence is the scaling
story. Quote fetches are keyed by *instrument*, never by user, so ten thousand
users watching RELIANCE cost one upstream call. Only the watermark comparison is
per-user, and that is pure computation over rows already fetched. The read path
gets more expensive as *instruments* are added, not as users are.

**Why each piece exists**

- **PostgreSQL** — every query here is relational (watchlist → watermark → latest
  observation → baseline). NoSQL would buy nothing.
- **Redis is provisioned but not yet used.** Quote deduplication and the
  single-flight lock currently run in-process (a TTL dict plus a
  `threading.Lock` in `service.py`), which is correct for a single instance and
  wrong for several. Redis is in `docker-compose.yml` with persistence disabled
  because that is the one thing it would hold, and nothing in it would be
  un-reconstructible from Postgres. Moving the lock and the TTL cache behind it
  is what makes the API horizontally scalable, and it is the next change I would
  make. Claiming it already does that job would be easier and untrue.
- **No Celery, no Kafka, no microservices.** There is one periodic refresh and no
  fan-out. A lock plus a TTL is 40 lines and correct. Adding a broker and a worker
  fleet for one job would be complexity I could not justify.
- **Scoring is pure functions.** No database, no clock, no globals — which is why
  72 tests covering every edge case run in 0.1 seconds.
- **The frontend is one static file.** No bundler, no `node_modules`, no second
  dev server, no CORS in production. For a home screen that is one list with three
  sections, a build toolchain would be weight without a job, and the whole product
  deploys as a single artifact.

---

## Live mode and replay mode

Two implementations behind one protocol. The pipeline downstream cannot tell them
apart — seeded data travels the identical code path as live data, because a demo
that bypasses the real system proves nothing.

**The fixtures are real NSE bars**, recorded from the live market on 31 Aug –
4 Sep 2026: 233,301 one-minute bars across 33 instruments. Replay controls *when*
they are served, never *what* they contain. Nothing here is synthetic, and the UI
says so on screen.

Replay exists because the market was closed for most of the build window, and a
returning-user demo needs a guaranteed interesting change at a controllable
moment. Set `DATA_MODE=LIVE` in `.env` to fetch from yfinance instead.

---

## Setup

Requires Python 3.11+ and Docker.

```bash
git clone <repo> && cd since
cp .env.example .env

docker compose up -d                    # Postgres + Redis, schema auto-applied

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

python tools/recorder.py history        # 90 days of daily bars -> baselines
python tools/recorder.py session        # intraday bars -> replay fixtures
python -m tools.seed                    # instruments + baselines into Postgres

uvicorn app.api:app --reload
```

Open **http://localhost:8000**.

### Verify it works

```bash
pytest tests/ -q          # 72 tests, no database needed
python -m tools.smoke     # walks the whole returning-user scenario end to end
python -m tools.calibrate # re-derives the threshold firing rates from history
```

### Environment

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql://since:since_dev_password@localhost:5433/since` | Port 5433 avoids colliding with a local Postgres |
| `REDIS_URL` | `redis://localhost:6380/0` | Same reasoning |
| `DATA_MODE` | `REPLAY` | `LIVE` fetches from yfinance |

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/digest` | Everything the home screen needs, one call |
| `POST` | `/api/watchlist` | Add an instrument, optional price level |
| `DELETE` | `/api/watchlist/{id}` | Remove |
| `POST` | `/api/acknowledge` | Advance watermarks — the only write that does |
| `GET` | `/api/instruments?q=` | Search |
| `GET` | `/api/health` | Liveness and row counts |
| `GET` | `/api/demo/span` | Bounds of the replay clock |

In replay mode every endpoint accepts `?at=<ISO timestamp>` to move the clock.
Live mode ignores it — otherwise the parameter would be a way to ask the system to
lie about the present.

`/api/digest` is one request rather than one per stock. Twenty round trips for a
twenty-stock watchlist is the usual reason these screens feel slow, and it makes
the tier counts inconsistent while they trickle in.

Interactive docs at `/docs`.

---

## Known limitations

Stated rather than hidden. Each is a real gap.

1. **Authentication is a trusted header.** The caller sends `X-User` and the server
   believes it. What *does* exist is the boundary: every query is scoped by
   `user_id` in SQL, so one user's watermarks cannot leak into another's digest.
   Sessions would slot in above that line without touching anything below it.
2. **Corporate actions are a heuristic, not a feed.** There is no corporate actions
   data source, so splits are inferred from a large move with no volume behind it
   or a price ratio matching a known split. It fails safe — on suspicion it stays
   silent rather than raising a false alarm. `instrument_baselines` has an unused
   `adjustment_factor` column ready for a real feed.
3. **Volume proration assumes even accrual.** Real intraday volume is U-shaped,
   heavy at the open and close. Scaling linearly by session progress is an
   approximation; a proper volume curve is the correct refinement.
4. **Baselines are seeded once from the end of the recorded window.** Replaying
   Monday morning uses Friday's `prev_close` as a baseline — lookahead. It only
   affects instruments the user has never acknowledged, since acknowledged ones
   compare against the watermark. The fix is a rolling baseline per date.
5. **No market-relative adjustment yet.** The index (`^NSEI`) is recorded and the
   design accounts for it, but subtracting the market's own move from each stock is
   not implemented. It would remove the largest remaining source of false signal:
   a stock that fell only because everything fell.
6. **yfinance is unofficial.** Rate-limited and occasionally wrong. It is behind
   the provider protocol precisely so it can be replaced without touching anything
   else.

## Deliberately not built

Real-time WebSocket tickers · push notifications · any LLM · a charting library ·
microservices · Kafka · Kubernetes · OAuth · portfolio P&L · a news feed.

Each was considered and rejected because it would have added surface area without
answering the question the brief actually asks. The word "smart" does not require
a model — it requires knowing which signal is worth computing.

---

## Financial responsibility

This product helps you decide **what to look at**, never what to do. Nothing in it
says buy, sell, opportunity, target or recommend — a test scans every generated
message for that vocabulary and fails the build if it appears.

The distinction is deliberate: prioritising information is not the same as giving
investment advice, and a watchlist at a broker should be very clear about which
one it is doing.
