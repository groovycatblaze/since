# Deploying, and the demo video

---

## Part 1 — Deploy to Railway (~20 minutes)

Railway reads the Dockerfile, provisions Postgres, and injects `DATABASE_URL`
and `PORT` automatically. Nothing in the repo needs to change.

### 1. Push the deployment files

```powershell
git add .
git commit -m "Add Dockerfile and idempotent database bootstrap for deployment"
git push
```

### 2. Create the project

1. Go to **railway.app**, sign in with GitHub.
2. **New Project → Deploy from GitHub repo → since**
3. Railway detects the Dockerfile and starts building. Let it run; it will fail
   on the first attempt because there is no database yet. That is expected.

### 3. Add Postgres

1. In the project canvas: **+ New → Database → Add PostgreSQL**
2. Click your **since** service → **Variables** tab → **New Variable** →
   **Add Reference** → pick the Postgres service's `DATABASE_URL`.

   Railway's own variable is named `DATABASE_URL`, which is exactly what
   `app/config.py` reads. No code change needed.

3. Add one more variable manually:

   | Name | Value |
   |---|---|
   | `DATA_MODE` | `REPLAY` |

### 4. Redeploy and get the URL

1. **Deployments → Redeploy**
2. Watch the logs. You want to see:

   ```
   [init] applying 001_schema.sql
   [init] schema applied
   [init] seeding instruments and baselines from data/history
   Seeded 33 instruments, skipped 0
   INFO: Application startup complete.
   ```

3. **Settings → Networking → Generate Domain**

That URL goes in the submission's **Demo Link** field.

### 5. Verify before you paste it anywhere

Open `https://<your-domain>/api/health`. You want:

```json
{"status":"ok","mode":"REPLAY","instruments":33,"observations":0}
```

Then open the root URL, add `HDFCBANK.NS` and `ADANIENT.NS`, set the clock to
31-08-2026 10:15, mark as seen, press Latest. If it re-ranks, you are live.

**If `instruments` is 0:** `data/history` did not make it into the image. Check
that `data/` is not in `.dockerignore` and that the history JSON files are
committed. They are gitignored by default — you may need to force-add them:

```powershell
git add -f data/history data/fixtures
git commit -m "Ship recorded NSE data so the deployment is self-contained"
git push
```

That is a deliberate exception to the gitignore: the deployed build has to be
self-contained, and 6MB of recorded bars is a reasonable price for a demo that
cannot fail because a third-party API is down.

### If Railway's free tier blocks you

**Render** works the same way: New → Web Service → Docker → add a PostgreSQL
instance → copy its Internal Database URL into `DATABASE_URL`. Slower cold
starts on the free tier, so open the link once before any live demo.

---

## Part 2 — The video (3 minutes)

Record with the deployed URL, not localhost. Keep the browser at 100% zoom.

### The script

**0:00–0:20 — the problem**

> "Here are two stocks. Both moved about two and a half percent today. One of
> them is a normal Tuesday, the other is the most unusual thing that stock has
> done in a month. Every watchlist I've used shows those identically."

**0:20–0:50 — the idea**

> "Since ranks by surprise, not by size. Each row is scored against that
> stock's own normal daily range, so this bar" — point at the range bar —
> "shows where the move actually falls. The two notches are the thresholds:
> past the first one it's worth noting, past the second it needs a look."

**0:50–1:40 — the returning-user flow**

Set the clock to 31 Aug 10:15. Press **Mark all as seen**. Press **Latest**.

> "I'm marking these as seen on the 31st, then coming back four days later.
> Everything is now measured from the moment I acknowledged, not from the
> market open. The sparkline on each row is the exact window I missed.
>
> And it's only that button that moves my place. Loading the page doesn't,
> refreshing doesn't. If you glance at a watchlist and get distracted, you
> shouldn't lose your diff."

**1:40–2:20 — one decision, told properly**

> "The obvious formula subtracts an expected return before dividing by
> volatility. I built that first and it produced z-scores that disagreed in
> sign with the price move — a stock up one percent reading as negative.
>
> Thirty days of data is plenty to estimate volatility and nowhere near enough
> to estimate a mean return; the standard error on the mean exceeds the mean.
> So I removed the drift term. Volatility is estimable at short horizons.
> Drift isn't."

**2:20–2:45 — restraint and honesty**

Show the quiet state, or a `CLOSED` freshness row.

> "It also has to be able to say nothing happened. The thresholds are
> calibrated on ninety days of recorded NSE history — needs-a-look fires on
> five percent of stock-days, not thirty.
>
> And stale data is shown but never scored. Market-closed isn't stale: Friday's
> close on a Saturday evening is the most current price that exists."

**2:45–3:00 — close**

> "No LLM, no WebSockets, no charting library. The word 'smart' didn't need a
> model — it needed knowing which signal was worth computing. The README lists
> six known limitations and the ten things I deliberately didn't build."

### Recording notes

- **Windows + G** opens Xbox Game Bar, which records the screen with audio. No
  install needed. Or use Loom.
- One take is fine. Small stumbles read as human; a polished voiceover over a
  screen recording reads as a marketing video, which is not what is being
  judged here.
- **Do not narrate the architecture.** Show the product, then one decision in
  real depth. Depth on one thing beats coverage of ten.
- Upload unlisted to YouTube or share the Loom link. Test the link in a private
  window before pasting it into the form.

---

## Part 3 — Final submission checklist

| Field | Value |
|---|---|
| Title | From `SUBMISSION.md` |
| Description | 100-word pitch, then supporting detail |
| Theme | Build a Smart Market Watchlist |
| Snapshots | Full page, close-up row, calibration output |
| Video URL | Your unlisted link |
| Demo Link | Your Railway domain |
| Repository URL | `https://github.com/groovycatblaze/since` |
| Source Code | `since.zip` |
| Instructions to Run | From `SUBMISSION.md` |

Before submitting, confirm:

- [ ] The deployed URL loads in a **private browsing window** (catches
      "works because I'm logged in" and cached-asset problems)
- [ ] The video link plays for someone who is not you
- [ ] `since.zip` contains `README.md`, `requirements.txt` and `data/`, and does
      **not** contain `.env`
- [ ] `pytest tests/ -q` passes from a clean clone
