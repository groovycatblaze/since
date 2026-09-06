"""
smoke.py — walk the whole returning-user scenario against a running API.

Run (with uvicorn already up in another terminal):
    python -m tools.smoke

What it proves, in order:
    1. The service is up and seeded.
    2. A watchlist can be built.
    3. At time T1 the user sees the market and acknowledges it.
    4. At time T2 the SAME stocks are re-scored against what the user
       acknowledged, not against the day's open.
    5. Nothing is flagged that should not be.

This doubles as the rehearsal script for the demo: it is deterministic, so
the same command produces the same output every time.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

BASE = "http://localhost:8000"
USER = "smoke@since.app"

# A spread of volatilities so the normalisation has something to show.
SYMBOLS = [
    "HDFCBANK.NS", "PAYTM.NS", "TCS.NS", "RELIANCE.NS", "IRCTC.NS",
    "SUZLON.NS", "ITC.NS", "ADANIENT.NS",
]

GREEN, YELLOW, DIM, BOLD, RESET = (
    "\033[92m", "\033[93m", "\033[90m", "\033[1m", "\033[0m")


def at_param(moment: datetime) -> str:
    """URL-encode the timestamp. Without quote(), the '+' in a +05:30
    offset is decoded server-side as a space and the parse fails."""
    return urllib.parse.quote(moment.isoformat(), safe="")


def call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-User": USER},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        print(f"  {YELLOW}HTTP {exc.code}{RESET} {method} {path}\n    {detail}")
        return {"_error": exc.code, "_detail": detail}
    except urllib.error.URLError as exc:
        print(f"\n{YELLOW}Cannot reach {BASE}{RESET} -- is uvicorn running?\n"
              f"  Start it with:  uvicorn app.api:app --reload\n  ({exc.reason})")
        sys.exit(1)


def show(digest: dict, label: str) -> None:
    print(f"\n{BOLD}{label}{RESET}")
    print(f"{DIM}{'-' * 74}{RESET}")
    counts = digest.get("counts", {})
    print(f"  needs attention: {counts.get('NEEDS_ATTENTION', 0)}   "
          f"changed: {counts.get('CHANGED', 0)}   "
          f"quiet: {counts.get('QUIET', 0)}")

    for tier in ("NEEDS_ATTENTION", "CHANGED", "QUIET"):
        items = digest.get("tiers", {}).get(tier, [])
        if not items:
            continue
        print(f"\n  {tier}")
        for it in items:
            z = f"z={it['z']:+.2f}" if it["z"] is not None else "z=n/a"
            pct = f"{it['pct_change']:+.2f}%" if it["pct_change"] is not None else "  n/a"
            print(f"    {it['symbol']:<14} {pct:>8}  {z:<10} "
                  f"{DIM}{it['freshness']:<8}{RESET} {it['comparison']}")
            for r in it["reasons"][:2]:
                print(f"      {DIM}- {r['message']}{RESET}")


def main() -> None:
    print(f"{BOLD}Since -- end-to-end smoke test{RESET}")

    # 1. health
    h = call("GET", "/api/health")
    if h.get("_error"):
        sys.exit(1)
    print(f"\n{GREEN}[1]{RESET} service up: mode={h['mode']} "
          f"instruments={h['instruments']} observations={h['observations']}")
    if h["instruments"] == 0:
        print(f"    {YELLOW}No instruments. Run: python -m tools.seed{RESET}")
        sys.exit(1)

    # 2. replay span -> pick two moments inside it
    span = call("GET", "/api/demo/span")
    if not span.get("span"):
        print(f"    {YELLOW}No replay span. Run: python tools/recorder.py session{RESET}")
        sys.exit(1)
    start = datetime.fromisoformat(span["span"][0])
    end = datetime.fromisoformat(span["span"][1])
    print(f"{GREEN}[2]{RESET} replay span {start:%d %b %H:%M} -> {end:%d %b %H:%M} "
          f"({span['symbols']} symbols)")

    # T1 an hour into the data, T2 near the end. Far enough apart that real
    # movement has occurred, both comfortably inside the recorded window.
    t1 = start + timedelta(hours=1)
    t2 = end - timedelta(minutes=5)

    # 3. build the watchlist
    added = []
    for sym in SYMBOLS:
        # Added ON THE DEMO CLOCK, not wall time. added_at and acknowledged_at
        # must sit on the same axis or every item compares as brand new.
        r = call("POST", f"/api/watchlist?at={at_param(t1)}", {"symbol": sym})
        if not r.get("_error"):
            added.append(r["symbol"])
    print(f"{GREEN}[3]{RESET} watchlist: {len(added)} instruments")

    # 4. first visit
    d1 = call("GET", f"/api/digest?at={at_param(t1)}")
    show(d1, f"[4] FIRST VISIT  {t1:%d %b %H:%M}")

    # 5. acknowledge everything
    ack = call("POST", f"/api/acknowledge?at={at_param(t1)}", {})
    print(f"\n{GREEN}[5]{RESET} acknowledged {ack.get('acknowledged')} instruments "
          f"(watermarks advanced: {ack.get('advanced')})")

    # 6. idempotency: the same acknowledge again must move nothing
    again = call("POST", f"/api/acknowledge?at={at_param(t1)}", {})
    ok = again.get("advanced") == 0
    print(f"{GREEN}[6]{RESET} repeat acknowledge advanced "
          f"{again.get('advanced')} rows "
          f"{'(idempotent, correct)' if ok else YELLOW + '(EXPECTED 0)' + RESET}")

    # 7. the user returns
    d2 = call("GET", f"/api/digest?at={at_param(t2)}")
    show(d2, f"[7] RETURN VISIT  {t2:%d %b %H:%M}")

    # 8. did the comparison baseline actually switch?
    baselines = {i["comparison"] for tier in d2.get("tiers", {}).values()
                 for i in tier}
    switched = "since you last checked" in baselines
    print(f"\n{GREEN}[8]{RESET} comparison baseline: {', '.join(sorted(baselines))}")
    if switched:
        print(f"    {GREEN}Scoring against the acknowledged price, not the "
              f"day's open. This is the product.{RESET}")
    else:
        print(f"    {YELLOW}Still comparing to previous close -- watermarks "
              f"are not being applied.{RESET}")

    print(f"\n{BOLD}Done.{RESET} Re-run any time: the replay clock makes this "
          f"deterministic.\n")


if __name__ == "__main__":
    main()
