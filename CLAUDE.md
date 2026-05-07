# Flood Risk — Phase 1: scrape river/discharge data into PostGIS Supabase.

## Stack
Python 3.10, psycopg2, earthengine-api, pandas, requests, python-dotenv.

## DB
- Conn string in `SUPABASE_CONN_STRING` env var (load via `.env`, never hardcode).
- Always: `with psycopg2.connect(SUPABASE_CONN_STRING) as conn:`
- SQL params use `%(name)s` — never f-strings in SQL.

## Scripts
- Every script has `--test` flag (single-record run).
- Print progress to stdout (terminal, no notebook).
- Exit 0 on success, 1 on failure.

## Layout
- `agents/` — extraction scripts
- `tests/` — validation
- `instructions/` — task briefs
