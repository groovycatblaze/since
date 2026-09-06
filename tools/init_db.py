"""
init_db.py — make a fresh database ready, idempotently.

Runs on every container start. Locally, docker-compose applies the schema via
docker-entrypoint-initdb.d, but a hosted Postgres arrives empty with no such
hook, so the application has to be able to bootstrap itself.

Idempotent on purpose: a platform may restart the container at any time, and a
start-up script that only works on a truly empty database is a script that
fails on the second deploy.

    python -m tools.init_db
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import PROJECT_ROOT, settings  # noqa: E402

SCHEMA = PROJECT_ROOT / "db" / "001_schema.sql"


def tables_exist(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'instruments'"
        )
        return cur.fetchone()[0] > 0


def instruments_seeded(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM instruments")
        return cur.fetchone()[0]


def main() -> None:
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = True

    if tables_exist(conn):
        print("[init] schema already present")
    else:
        print(f"[init] applying {SCHEMA.name}")
        with conn.cursor() as cur:
            cur.execute(SCHEMA.read_text(encoding="utf-8"))
        print("[init] schema applied")

    count = instruments_seeded(conn)
    conn.close()

    if count:
        print(f"[init] {count} instruments already seeded")
        return

    print("[init] seeding instruments and baselines from data/history")
    from tools.seed import main as seed_main
    try:
        seed_main()
    except SystemExit:
        # seed() exits when data/history is missing. That is survivable: the
        # API still starts and reports empty counts on /api/health, which is a
        # far more diagnosable failure than a container that will not boot.
        print("[init] no recorded history found — starting with an empty database")


if __name__ == "__main__":
    main()
