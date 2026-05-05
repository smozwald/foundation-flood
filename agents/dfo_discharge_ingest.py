#!/usr/bin/env python3
"""
dfo_discharge_ingest.py — DFO Flood Observatory discharge + threshold ingestion.

Usage:
    python agents/dfo_discharge_ingest.py --test           # station 000257 only, print, no insert
    python agents/dfo_discharge_ingest.py --dry-run        # all stations, print, no insert
    python agents/dfo_discharge_ingest.py --station 000257 # single station
    python agents/dfo_discharge_ingest.py                  # full batch with checkpointing
"""

import argparse
import io
import os
import re
import sys
import uuid
from datetime import date, datetime, timedelta
import time

import numpy as np
import pandas as pd
from scipy import stats
import psycopg2
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import easyocr
    EASYOCR_AVAILABLE = True
except ImportError:
    EASYOCR_AVAILABLE = False

load_dotenv()

CONN_STRING = os.environ.get("SUPABASE_CONN_STRING")
if not CONN_STRING:
    print("ERROR: SUPABASE_CONN_STRING is not set.")
    sys.exit(1)

DFO_BASE_URL = "https://floodobservatory.colorado.edu"
STATION_LIST_URL = f"{DFO_BASE_URL}/wiki/DischargeFromSpace_Tab"
STATION_PAGE_URL = f"{DFO_BASE_URL}/wiki/Discharge:Station_{{id}}"

TEST_STATION_ID = "000257"
DATE_START = date(2015, 1, 1)
DATE_END = date(2025, 12, 31)

RETURN_PERIOD_LABELS = {
    1: "2-5 yr",
    2: "5-10 yr",
    3: "10-25 yr",
    4: "25-50 yr",
    5: "50+ yr",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (flood-risk-research/1.0)"}

# Lazy-loaded easyocr reader (initialisation is slow)
_ocr_reader = None

def get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        _ocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _ocr_reader


# ---------------------------------------------------------------------------
# Station list — HTML table on DFO wiki page
# ---------------------------------------------------------------------------

def fetch_station_list():
    """
    Parse the DFO wiki station list HTML table.
    Columns: Station link, Coordinates (lat, lon), RiverName, Country, ...
    Returns list of dicts: {station_id, river_name, country, lat, lon}
    """
    print(f"Fetching station list from {STATION_LIST_URL}")
    resp = requests.get(STATION_LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        raise ValueError("No table found on station list page")

    rows = table.find_all("tr")
    print(f"  Found {len(rows) - 1} station rows")

    records = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        # Cell 0: "Station 000257" with link
        m = re.search(r"(\d{6})", cells[0].get_text(strip=True))
        if not m:
            continue
        station_id = m.group(1)

        # Cell 1: "46.7, 29.7" (lat, lon)
        coords_text = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        coord_nums = re.findall(r"-?\d+(?:\.\d+)?", coords_text)
        lat = float(coord_nums[0]) if len(coord_nums) >= 1 else None
        lon = float(coord_nums[1]) if len(coord_nums) >= 2 else None

        river_name = cells[2].get_text(strip=True) if len(cells) > 2 else None
        country = cells[3].get_text(strip=True) if len(cells) > 3 else None

        records.append({
            "station_id": station_id,
            "river_name": river_name,
            "country": country,
            "lat": lat,
            "lon": lon,
        })

    print(f"  Parsed {len(records)} stations")
    return records


# ---------------------------------------------------------------------------
# Per-station page
# ---------------------------------------------------------------------------

def fetch_station_page(station_id):
    """Fetch per-station wiki page. Returns (html, final_url)."""
    url = STATION_PAGE_URL.format(id=station_id)
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text, resp.url


def find_csv_url(html):
    """Extract the 'download data' CSV link from station page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()
        if href.endswith(".csv") or "download" in text:
            return href
    return None


def find_threshold_image_url(html, page_url):
    """
    Station page has two images: (0) half-year chart, (1) entire-record chart.
    The entire-record chart shows flood category background bands with threshold values.
    Returns URL of the second image.
    """
    soup = BeautifulSoup(html, "html.parser")
    imgs = []
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src or src.startswith("data:"):
            continue
        if src.startswith("http"):
            imgs.append(src)
        elif src.startswith("/"):
            imgs.append(DFO_BASE_URL + src)
        else:
            imgs.append(page_url.rsplit("/", 1)[0] + "/" + src)

    chart_imgs = [
        u for u in imgs
        if re.search(r"\.(png|jpg|gif)$", u, re.IGNORECASE)
        and not re.search(r"pix|logo|icon|nav|arrow", u, re.IGNORECASE)
    ]

    if len(chart_imgs) >= 2:
        return chart_imgs[1]
    if chart_imgs:
        return chart_imgs[0]
    return None


# ---------------------------------------------------------------------------
# Discharge CSV
# ---------------------------------------------------------------------------

def fetch_and_parse_csv(csv_url):
    """Download discharge CSV, return list of (date, float|None)."""
    resp = requests.get(csv_url, headers=HEADERS, timeout=60)
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text), dtype=str)
    df.columns = [
        c.strip().lower().replace(" ", "_").replace("(", "").replace(")", "")
        for c in df.columns
    ]

    date_col = next(
        (c for c in df.columns if "date" in c or "time" in c), df.columns[0]
    )
    dis_col = next(
        (c for c in df.columns if any(k in c for k in ["discharge", "m3", "flow", "q"])),
        df.columns[1] if df.shape[1] > 1 else None,
    )
    if dis_col is None:
        raise ValueError(f"Cannot identify discharge column in: {list(df.columns)}")

    records = []
    for _, row in df.iterrows():
        try:
            obs_date = datetime.strptime(str(row[date_col]).strip(), "%Y-%m-%d").date()
        except ValueError:
            continue
        try:
            val = float(row[dis_col])
            dis = val if val >= 0 else None
        except (ValueError, TypeError):
            dis = None
        records.append((obs_date, dis))

    return records


# ---------------------------------------------------------------------------
# Threshold extraction — CSV Analysis
#
# The DFO "entire record" chart has horizontal coloured background bands:
#   purple  → category 5  (50+ yr)
#   pink    → category 4  (25-50 yr)
#   red     → category 3  (10-25 yr)
#   orange  → category 2  (5-10 yr)
#   yellow  → category 1  (2-5 yr)
#   white   → below all thresholds
#
# Strategy:
#   1. Download the CSV
#   2. Calculate if dataset is complete for GEV/Gumbel analysis
#   3. Will calculate thresholds based on entire dataset, using GEV where complete.
# ---------------------------------------------------------------------------

FLOOD_RETURN_PERIODS = {
    2:  1,
    5:  2,
    10: 3,
    25: 4,
    50: 5,
}

MIN_MONTHS_FOR_COMPLETE_YEAR = 9  # exclude partial years (e.g. current year)
MIN_YEARS_FOR_GEV            = 15  # below this, GEV MLE is too unstable → Gumbel
GEV_SHAPE_LIMIT              = 2.0 # |ξ| beyond this → degenerate fit → Gumbel


def _extract_annual_maxima(csv_bytes: bytes) -> pd.Series:
    """
    Parse a discharge CSV (Date, Discharge (m3/s)) and return a Series of
    annual maxima, excluding partial years with < MIN_MONTHS_FOR_COMPLETE_YEAR
    distinct months of data.
    """
    df = pd.read_csv(io.BytesIO(csv_bytes), parse_dates=["Date"])
    df.columns = ["date", "q"]
    df = df.dropna(subset=["q"])
    df["year"] = df["date"].dt.year

    month_coverage = df.groupby("year")["date"].apply(
        lambda s: s.dt.month.nunique()
    )
    complete = month_coverage[month_coverage >= MIN_MONTHS_FOR_COMPLETE_YEAR].index
    ams = (
        df[df["year"].isin(complete)]
        .groupby("year")["q"]
        .max()
        .dropna()
    )
    return ams


def _fit_and_compute(ams: pd.Series) -> tuple[dict, str]:
    """
    Fit GEV (if record long enough and shape sane) else Gumbel.
    Returns (thresholds_dict, method_label).
    """
    data = ams.values
    n    = len(data)
    method = None

    if n >= MIN_YEARS_FOR_GEV:
        try:
            xi, loc, scale = stats.genextreme.fit(data)
            if abs(xi) > GEV_SHAPE_LIMIT:
                raise ValueError(f"Degenerate shape ξ={xi:.3f}")
            # confirmed good fit — use GEV
            quantile = lambda T: float(
                stats.genextreme.ppf(1 - 1 / T, xi, loc=loc, scale=scale)
            )
            method = f"GEV-MLE (ξ={xi:.3f}, μ={loc:.2f}, σ={scale:.2f}, n={n})"
        except Exception:
            xi = None  # fall through to Gumbel

    if method is None:
        loc, scale = stats.gumbel_r.fit(data)
        quantile = lambda T: float(
            loc + scale * (-np.log(-np.log(1 - 1 / T)))
        )
        method = f"Gumbel-MLE (μ={loc:.2f}, β={scale:.2f}, n={n})"

    thresholds = {
        key: round(quantile(T), 2)
        for T, key in FLOOD_RETURN_PERIODS.items()
    }
    return thresholds, method


def compute_thresholds_from_csv(csv_bytes: bytes) -> tuple[dict, str]:
    """
    Public entry point.  Drop-in for the image-OCR path.

    Returns:
        thresholds  – {category: m3/s}  keys match existing schema
        derived_from – human-readable string describing the method used
    """
    ams = _extract_annual_maxima(csv_bytes)
    if len(ams) < 3:
        return {}, "csv_generated: insufficient annual maxima"

    thresholds, method_label = _fit_and_compute(ams)
    derived_from = f"csv_generated using {method_label}"
    return thresholds, derived_from

def get_thresholds(html, page_url, csv_bytes: bytes | None = None):
    """
    Extract flood thresholds. Tries CSV-based frequency analysis. 
    Will return empty if this fails.
    Returns ({category: m3/s}, derived_from_str).
    """
    if csv_bytes:
        thresholds, derived_from = compute_thresholds_from_csv(csv_bytes)
        if thresholds:
            return thresholds, derived_from
    print("    WARNING: threshold extraction failed — thresholds will be empty")
    return {}, "unknown"



# ---------------------------------------------------------------------------
# Time series and event derivation
# ---------------------------------------------------------------------------

def build_full_ts(raw_records, thresholds):
    """
    Build complete daily TS from DATE_START to DATE_END.
    Fills gaps with NULL discharge. Computes threshold_exceeded_category.
    """
    by_date = dict(raw_records)
    sorted_cats = sorted(thresholds.keys(), reverse=True)
    rows = []
    current = DATE_START
    while current <= DATE_END:
        dis = by_date.get(current)
        exceeded = None
        if dis is not None and thresholds:
            for cat in sorted_cats:
                if dis >= thresholds[cat]:
                    exceeded = cat
                    break
        rows.append({
            "obs_date": current,
            "discharge_m3s": dis,
            "threshold_exceeded_category": exceeded,
            "qc_flag": "raw",
        })
        current += timedelta(days=1)

    return rows


def derive_flood_events(ts_rows):
    """
    Derive flood events from consecutive above-threshold days.
    """
    events = []
    event_days = []

    def close_event(days):
        valid = [r for r in days if r["discharge_m3s"] is not None]
        peak = max(valid, key=lambda r: r["discharge_m3s"]) if valid else None
        return {
            "event_id": str(uuid.uuid4()),
            "flood_start": days[0]["obs_date"],
            "flood_end": days[-1]["obs_date"],
            "duration_days": (days[-1]["obs_date"] - days[0]["obs_date"]).days + 1,
            "peak_discharge_m3s": peak["discharge_m3s"] if peak else None,
            "peak_date": peak["obs_date"] if peak else None,
            "max_category": max(
                r["threshold_exceeded_category"]
                for r in days if r["threshold_exceeded_category"] is not None
            ),
        }

    for row in ts_rows:
        if row["threshold_exceeded_category"] is not None:
            event_days.append(row)
        elif event_days:
            events.append(close_event(event_days))
            event_days = []

    if event_days:
        events.append(close_event(event_days))

    return events


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def get_or_create_data_source(cur):
    cur.execute(
        "SELECT source_id FROM data_sources WHERE product_name = %(name)s",
        {"name": "DFO Flood Observatory"},
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """
        INSERT INTO data_sources (product_name, version_tag)
        VALUES (%(name)s, %(tag)s)
        RETURNING source_id
        """,
        {"name": "DFO Flood Observatory", "tag": f"scraped:{date.today().isoformat()}"},
    )
    return cur.fetchone()[0]


def upsert_station(cur, source_id, dfo_station_id, station_name, lat, lon):
    cur.execute(
        """
        INSERT INTO discharge_stations (dfo_station_id, station_name, geom, source_id)
        VALUES (
            %(dfo_id)s,
            %(name)s,
            ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
            %(src)s
        )
        ON CONFLICT (dfo_station_id) DO UPDATE
            SET station_name = EXCLUDED.station_name,
                geom         = EXCLUDED.geom
        RETURNING station_id
        """,
        {"dfo_id": dfo_station_id, "name": station_name, "lat": lat, "lon": lon, "src": source_id},
    )
    return cur.fetchone()[0]


def update_station_dates(cur, station_uuid, record_start, record_end):
    cur.execute(
        """
        UPDATE discharge_stations
        SET record_start = %(start)s, record_end = %(end)s
        WHERE station_id = %(sid)s
        """,
        {"start": record_start, "end": record_end, "sid": station_uuid},
    )


def insert_thresholds(cur, station_uuid, thresholds, derived_from):
    for cat, value in thresholds.items():
        if not (1 <= cat <= 5):
            continue
        cur.execute(
            """
            INSERT INTO flood_thresholds
                (station_id, category, return_period_label, discharge_threshold_m3s, derived_from)
            VALUES (%(sid)s, %(cat)s, %(label)s, %(val)s, %(derived_from)s)
            ON CONFLICT (station_id, category) DO UPDATE
                SET discharge_threshold_m3s = EXCLUDED.discharge_threshold_m3s,
                    derived_from            = EXCLUDED.derived_from
            """,
            {
                "sid": station_uuid,
                "cat": cat,
                "label": RETURN_PERIOD_LABELS.get(cat, f"category_{cat}"),
                "val": value,
                "derived_from": derived_from,
            },
        )


from psycopg2.extras import execute_values

def insert_discharge_ts(cur, station_uuid, source_id, ts_rows):
    execute_values(cur, """
        INSERT INTO discharge_ts
            (station_id, obs_date, discharge_m3s, discharge_anomaly_pct,
             threshold_exceeded_category, qc_flag, source_id)
        VALUES %s
        ON CONFLICT (station_id, obs_date) DO NOTHING
    """,
    [
        (
            station_uuid,
            row["obs_date"],
            row["discharge_m3s"],
            None,
            row["threshold_exceeded_category"],
            row["qc_flag"],
            source_id,
        )
        for row in ts_rows
    ])

def insert_flood_events(cur, station_uuid, events):
    for ev in events:
        cur.execute(
            """
            INSERT INTO flood_events
                (event_id, station_id, flood_start, flood_end,
                 peak_discharge_m3s, peak_date, max_category, detection_method)
            VALUES
                (%(eid)s, %(sid)s, %(start)s, %(end)s,
                 %(peak_q)s, %(peak_d)s, %(max_cat)s, 'threshold_exceedance')
            ON CONFLICT (event_id) DO NOTHING
            """,
            {
                "eid": ev["event_id"],
                "sid": station_uuid,
                "start": ev["flood_start"],
                "end": ev["flood_end"],
                "peak_q": ev["peak_discharge_m3s"],
                "peak_d": ev["peak_date"],
                "max_cat": ev["max_category"],
            },
        )

def get_already_processed(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dfo_station_id FROM discharge_stations WHERE record_end IS NOT NULL"
        )
        return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Single station pipeline
# ---------------------------------------------------------------------------

# ── process_station ───────────────────────────────────────────────────────────
def process_station(rec, dry_run, conn, source_id):
    sid        = rec["station_id"]
    river_name = rec.get("river_name") or f"Station {sid}"
    lat, lon   = rec.get("lat"), rec.get("lon")

    if lat is None or lon is None:
        return 0, 0, {}, "Missing coordinates"

    try:
        html, final_url = fetch_station_page(sid)
    except requests.RequestException as e:
        return 0, 0, {}, f"Page fetch failed: {e}"

    # ── fetch CSV first so get_thresholds can use it ──────────────────────────
    csv_url = find_csv_url(html)
    csv_bytes = None
    if csv_url:
        print(f"  CSV: {csv_url}")
        try:
            csv_bytes = fetch_csv_bytes(csv_url)   # new helper — see below
            time.sleep(2)
        except Exception as e:
            print(f"  CSV fetch failed: {e}")

    # pass raw bytes into get_thresholds; it handles None gracefully
    thresholds, derived_from = get_thresholds(html, final_url, csv_bytes)
    if not thresholds:
        return 0, 0, {}, "Threshold extraction failed"

    if csv_bytes is None:
        return 0, 0, thresholds, "No discharge CSV URL found on station page"

    try:
        raw_records = parse_csv_from_bytes(csv_bytes)   # refactored — see below
    except Exception as e:
        return 0, 0, thresholds, f"CSV parse failed: {e}"

    all_dates = [d for d, _ in raw_records]
    record_start = min(all_dates) if all_dates else None
    record_end = max(all_dates) if all_dates else None

    filtered = [(d, v) for d, v in raw_records if d >= DATE_START]
    ts_rows = build_full_ts(filtered, thresholds)
    events = derive_flood_events(ts_rows)

    print(f"  Raw rows: {len(raw_records)} | TS rows (2015+): {len(ts_rows)} | "
          f"Thresholds: {len(thresholds)} | Events: {len(events)}")

    if dry_run:
        return len(ts_rows), len(events), thresholds, None

    with conn.cursor() as cur:
        station_uuid = upsert_station(cur, source_id, sid, river_name, lat, lon)
        if thresholds:
            insert_thresholds(cur, station_uuid, thresholds, derived_from)
        insert_discharge_ts(cur, station_uuid, source_id, ts_rows)
        if all_dates:
            update_station_dates(cur, station_uuid, record_start, record_end)
        if events:
            insert_flood_events(cur, station_uuid, events)

    conn.commit()
    return len(ts_rows), len(events), thresholds, None

import time

def fetch_csv_bytes(csv_url: str, retries: int = 5) -> bytes:
    for attempt in range(retries):
        try:
            resp = requests.get(csv_url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 429:
                wait = 2 ** attempt
                print(f"  Rate limited — waiting {wait}s before retry {attempt+1}/{retries}")
                time.sleep(wait)
            else:
                raise
    raise requests.exceptions.HTTPError(f"Failed after {retries} retries: {csv_url}")

def parse_csv_from_bytes(csv_bytes: bytes) -> list[tuple]:
    """
    Replaces fetch_and_parse_csv(url).
    Parses bytes → [(date, value), ...] matching your existing return type.
    """
    import io
    df = pd.read_csv(io.BytesIO(csv_bytes), parse_dates=["Date"])
    df.columns = ["date", "q"]
    df = df.dropna(subset=["q"])
    return list(zip(df["date"].dt.date, df["q"]))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DFO Flood Observatory ingestion pipeline")
    parser.add_argument("--test", action="store_true",
                        help="Station 000257 only; print all parsed values; do not insert")
    parser.add_argument("--dry-run", action="store_true",
                        help="All stations; print; do not insert")
    parser.add_argument("--station", metavar="ID",
                        help="Run for a single station ID")
    args = parser.parse_args()

    dry_run = args.test or args.dry_run

    try:
        records = fetch_station_list()
    except Exception as e:
        print(f"ERROR fetching station list: {e}")
        sys.exit(1)

    if args.test:
        records = [r for r in records if r["station_id"] == TEST_STATION_ID]
        if not records:
            records = [{"station_id": TEST_STATION_ID, "river_name": None,
                        "lat": None, "lon": None}]
        print(f"TEST mode: station {TEST_STATION_ID} only")
    elif args.station:
        target = args.station.zfill(6)
        records = [r for r in records if r["station_id"] == target]
        if not records:
            print(f"ERROR: station {args.station} not found in station list")
            sys.exit(1)

    total = len(records)
    processed = skipped = errors = 0

    if dry_run:
        conn = None
        source_id = None
        already_done = set()
    else:
        try:
            conn = psycopg2.connect(CONN_STRING)
        except psycopg2.Error as e:
            print(f"ERROR: DB connection failed: {e}")
            sys.exit(1)
        with conn.cursor() as cur:
            source_id = get_or_create_data_source(cur)
        conn.commit()
        print(f"DFO source_id: {source_id}")
        already_done = get_already_processed(conn) if not (args.test or args.station) else set()

    for i, rec in enumerate(records, start=1):
        sid = rec["station_id"]

        if not (args.test or args.station) and not dry_run and sid in already_done:
            print(f"[{i}/{total}] {sid} — skipped (already processed)")
            skipped += 1
            continue

        print(f"[{i}/{total}] {sid} ({rec.get('river_name', '?')}) — processing")
        try:
            ts_n, ev_n, thresholds, err = process_station(
                rec, dry_run=dry_run, conn=conn, source_id=source_id
            )
            if err:
                print(f"[{i}/{total}] {sid} — error: {err}")
                errors += 1
            else:
                tag = "dry-run" if dry_run else "ok"
                print(f"[{i}/{total}] {sid} — {tag} | ts={ts_n} events={ev_n}")
                if args.test:
                    if thresholds:
                        print("  Thresholds:")
                        for cat in sorted(thresholds):
                            print(f"    [{cat}] {RETURN_PERIOD_LABELS.get(cat, '?')}: "
                                  f"{thresholds[cat]:.1f} m3/s")
                    else:
                        print("  Thresholds: none extracted")
                processed += 1
        except Exception as e:
            print(f"[{i}/{total}] {sid} — error: {e}")
            if conn and not dry_run:
                try:
                    conn.rollback()
                except Exception:
                    pass
            errors += 1

    if conn:
        conn.close()

    print(f"\nSummary: {processed} processed / {skipped} skipped / {errors} errors")
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
