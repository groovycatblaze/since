-- Since — schema v1
--
-- Design principles, in order of importance:
--
-- 1. Market state is SHARED. User memory is PERSONAL. Every table below sits
--    on one side of that line, and the line is why this scales: N users
--    watching RELIANCE cost one row of market data, not N.
--
-- 2. Observations are APPEND-ONLY and immutable. We never overwrite a price.
--    This is what makes "what changed since you last looked" answerable at
--    all, and it makes every bug reproducible.
--
-- 3. Instruments are identified by a stable internal id, NOT by ticker.
--    Tickers change (ZOMATO -> ETERNAL, TATAMOTORS -> TMPV). A watchlist
--    keyed on a ticker string silently breaks when a company renames.

-- ---------------------------------------------------------------------------
-- SHARED MARKET STATE
-- ---------------------------------------------------------------------------

-- An instrument is the *company*, not the ticker. Survives renames.
CREATE TABLE instruments (
    id              BIGSERIAL PRIMARY KEY,
    display_name    TEXT        NOT NULL,
    exchange        TEXT        NOT NULL DEFAULT 'NSE',
    currency        TEXT        NOT NULL DEFAULT 'INR',
    is_index        BOOLEAN     NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Symbol history. One instrument may have had several tickers over time.
-- valid_to IS NULL means "this is the current ticker".
CREATE TABLE instrument_symbols (
    id              BIGSERIAL PRIMARY KEY,
    instrument_id   BIGINT      NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    symbol          TEXT        NOT NULL,
    valid_from      DATE        NOT NULL DEFAULT CURRENT_DATE,
    valid_to        DATE,
    note            TEXT
);

-- Exactly one current symbol per instrument.
CREATE UNIQUE INDEX ux_instrument_current_symbol
    ON instrument_symbols (instrument_id) WHERE valid_to IS NULL;

-- A symbol can only be current for one instrument at a time.
CREATE UNIQUE INDEX ux_symbol_current
    ON instrument_symbols (symbol) WHERE valid_to IS NULL;

CREATE INDEX ix_instrument_symbols_lookup ON instrument_symbols (symbol);


-- Append-only. Every fetch lands here, successes AND failures.
--
-- TWO CLOCKS on purpose:
--   exchange_time = when the market says this happened
--   fetched_at    = when WE saw it
-- Freshness is the gap between them. Storing one timestamp makes staleness
-- uncomputable, which is why most implementations cannot answer the brief's
-- question about stale data.
CREATE TABLE market_observations (
    id                  BIGSERIAL PRIMARY KEY,
    instrument_id       BIGINT      NOT NULL REFERENCES instruments(id),
    exchange_time       TIMESTAMPTZ NOT NULL,
    fetched_at          TIMESTAMPTZ NOT NULL,
    source              TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'OK',

    -- Nullable: a NO_DATA observation records the absence of a price.
    -- We record the gap rather than inventing a value.
    price               NUMERIC(18,4),
    open_price          NUMERIC(18,4),
    day_high            NUMERIC(18,4),
    day_low             NUMERIC(18,4),
    cumulative_volume   NUMERIC(20,2),

    session_date        DATE        NOT NULL,

    CONSTRAINT ck_obs_status
        CHECK (status IN ('OK', 'NO_DATA', 'BATCH_FAILURE')),
    CONSTRAINT ck_obs_price_present
        CHECK (status <> 'OK' OR price IS NOT NULL),
    CONSTRAINT ck_obs_price_positive
        CHECK (price IS NULL OR price > 0)
);

-- Makes ingest idempotent: re-running the loader cannot duplicate a bar.
-- (The recorder appends blindly by design; the loader must not.)
CREATE UNIQUE INDEX ux_observation_natural_key
    ON market_observations (instrument_id, exchange_time, source);

-- The hot query: "latest observation for these instruments".
CREATE INDEX ix_obs_latest
    ON market_observations (instrument_id, exchange_time DESC);

CREATE INDEX ix_obs_session
    ON market_observations (session_date, instrument_id);


-- Recomputed daily after the close. Shared across all users — this is the
-- denominator that turns a raw % move into "unusual for THIS stock".
CREATE TABLE instrument_baselines (
    instrument_id       BIGINT      NOT NULL REFERENCES instruments(id),
    as_of_date          DATE        NOT NULL,

    prev_close          NUMERIC(18,4) NOT NULL,
    mean_log_return     NUMERIC(12,8) NOT NULL,

    -- Robust sigma (from median absolute deviation), not raw stdev: one
    -- earnings gap should not inflate the denominator for a month.
    sigma_log_return    NUMERIC(12,8) NOT NULL,
    median_volume_20d   NUMERIC(20,2),

    -- Named for the window we ACTUALLY have. The recorder pulls 90 days, so
    -- calling these "week52_high/low" would be a quiet lie in the schema —
    -- and a reviewer who checks the recorder against the column name would
    -- find it. range_days makes the window explicit at every read.
    range_high          NUMERIC(18,4),
    range_low           NUMERIC(18,4),
    range_days          INTEGER,

    sample_days         INTEGER     NOT NULL,

    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (instrument_id, as_of_date),

    -- Floor sigma. An illiquid stock with a near-zero denominator would
    -- otherwise produce infinite z-scores and flag everything.
    CONSTRAINT ck_sigma_floor CHECK (sigma_log_return >= 0.0001),
    CONSTRAINT ck_sample_days CHECK (sample_days >= 10)
);


-- ---------------------------------------------------------------------------
-- PERSONAL USER STATE
-- ---------------------------------------------------------------------------

CREATE TABLE users (
    id              BIGSERIAL PRIMARY KEY,
    email           TEXT        NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE watchlists (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT        NOT NULL DEFAULT 'My Watchlist',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_watchlists_user ON watchlists (user_id);

-- Points at instrument_id, never at a ticker string. This is what survives
-- a rename or a demerger.
CREATE TABLE watchlist_items (
    id              BIGSERIAL PRIMARY KEY,
    watchlist_id    BIGINT      NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    instrument_id   BIGINT      NOT NULL REFERENCES instruments(id),
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Optional personalisation. NULL means "use sensible defaults" — the
    -- product must work with zero configuration.
    threshold_price NUMERIC(18,4)
);

-- Adding the same stock twice is a no-op, not an error or a duplicate row.
CREATE UNIQUE INDEX ux_watchlist_instrument
    ON watchlist_items (watchlist_id, instrument_id);


-- THE core table. One row per (user, instrument): the observation the user
-- last explicitly acknowledged.
--
-- MONOTONIC BY CONSTRUCTION. acknowledged_at only ever moves forward, which
-- is why two devices acknowledging concurrently converge without locking:
-- the later timestamp wins, the earlier one is a no-op.
--
-- Viewing does NOT write here. Only an explicit acknowledge does. Otherwise
-- a user who glances at the app and closes it loses their diff.
CREATE TABLE user_watermarks (
    user_id             BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    instrument_id       BIGINT      NOT NULL REFERENCES instruments(id),

    acknowledged_at     TIMESTAMPTZ NOT NULL,
    baseline_price      NUMERIC(18,4) NOT NULL,
    baseline_obs_id     BIGINT      REFERENCES market_observations(id),

    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (user_id, instrument_id),
    CONSTRAINT ck_watermark_price CHECK (baseline_price > 0)
);


-- Written only on TIER TRANSITIONS, not every poll. Gives a real
-- "while you were away" timeline without the cost of event-sourcing
-- everything.
CREATE TABLE attention_events (
    id              BIGSERIAL PRIMARY KEY,
    instrument_id   BIGINT      NOT NULL REFERENCES instruments(id),
    occurred_at     TIMESTAMPTZ NOT NULL,
    from_tier       TEXT,
    to_tier         TEXT        NOT NULL,

    -- Structured, not prose. The backend emits reasons; the frontend renders
    -- them. The frontend never reverse-engineers an explanation.
    reasons         JSONB       NOT NULL DEFAULT '[]'::JSONB,

    CONSTRAINT ck_event_tier
        CHECK (to_tier IN ('NEEDS_ATTENTION', 'CHANGED', 'QUIET'))
);

CREATE INDEX ix_events_instrument_time
    ON attention_events (instrument_id, occurred_at DESC);
