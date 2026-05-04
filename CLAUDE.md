# Flood Risk Project — Claude Code Context

## Project purpose
Satellite-based flood risk modelling for agricultural land.
Phase 1 is data collection — scraping river/discharge data and
storing it in a PostGIS Supabase database.

## Stack
- Python 3.10
- psycopg2 (direct Supabase PostgreSQL connection)
- Google Earth Engine Python API (earthengine-api)
- pandas, requests

## Database
Connection via SUPABASE_CONN_STRING environment variable.
Never hardcode credentials. Use python-dotenv to load a .env file locally.

## Code standards
- All SQL uses %(name)s psycopg2 placeholders — never f-strings in SQL
- Always use: with psycopg2.connect(SUPABASE_CONN_STRING) as conn
- Every script must have a --test flag that runs on a single record only
- Print progress clearly — this runs in a terminal, no notebook UI
- Scripts return exit code 0 on success, 1 on failure

## Repo layout
- agents/   — generated extraction scripts
- tests/    — validation scripts
- instructions/ — task briefs for Claude Code