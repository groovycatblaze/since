# Single-stage on slim. A multi-stage build would shave maybe 200MB off an
# image that is already small, and would cost debugging time this build does
# not have to spare.
FROM python:3.12-slim

# psycopg2-binary ships its own libpq, so no build toolchain is needed. That is
# the reason for choosing the binary wheel over psycopg2 here.
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY tools/ ./tools/
COPY db/ ./db/
COPY data/ ./data/

# The recorded NSE fixtures ship inside the image (~6MB). The alternative is
# fetching from yfinance at boot, which would make every cold start depend on
# an unofficial third-party API being up and not rate-limiting us. A demo that
# can fail because someone else's service is down is not a demo.

ENV DATA_MODE=REPLAY \
    LOG_LEVEL=INFO

EXPOSE 8000

# Bootstrap then serve. init_db is idempotent, so a platform restarting the
# container mid-deploy cannot corrupt anything or fail on the second run.
# Hosting platforms inject $PORT; default to 8000 for plain `docker run`.
CMD ["sh", "-c", "python -m tools.init_db && uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
