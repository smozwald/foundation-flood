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

import numpy as np
import pandas as pd
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
    1: "1.5-2 yr",
    2: "2-5 yr",
    3: "5-10 yr",
    4: "10-20 yr",
    5: "20+ yr",
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
# Threshold extraction — image analysis
#
# The DFO "entire record" chart has horizontal coloured background bands:
#   purple  → category 5  (20+ yr)
#   pink    → category 4  (10-20 yr)
#   red     → category 3  (5-10 yr)
#   orange  → category 2  (2-5 yr)
#   yellow  → category 1  (1.5-2 yr)
#   white   → below all thresholds
#
# Strategy:
#   1. OCR the left y-axis strip → pixel-y to discharge linear map.
#   2. Compute row-median RGB across the chart area.
#   3. Find the coloured region (sat > 12) → bottom = threshold_1.
#   4. Within that region, detect colour transitions (large jump in R-B or G
#      channel) → boundaries between categories = thresholds 2-5.
# ---------------------------------------------------------------------------

def _build_axis_scale(img_array):
    """
    OCR the left strip of the image to extract y-axis labels.
    Returns (slope, intercept) such that discharge = slope * pixel_y + intercept,
    or None if fewer than 2 labels found.
    """
    if not (PIL_AVAILABLE and EASYOCR_AVAILABLE):
        return None

    h = img_array.shape[0]
    img = Image.fromarray(img_array)
    left_strip = img.crop((0, 0, 300, h))
    left_strip.save("/tmp/_dfo_axis.png")

    reader = get_ocr_reader()
    results = reader.readtext("/tmp/_dfo_axis.png", detail=1)

    axis_points = []
    for bbox, text, conf in results:
        clean = text.replace(",", "").replace(" ", "")
        if re.match(r"^\d{3,6}$", clean) and conf > 0.4:
            val = int(clean)
            if 50 <= val <= 10_000_000:
                y_center = (bbox[0][1] + bbox[2][1]) / 2
                axis_points.append((y_center, val))

    if len(axis_points) < 2:
        return None

    ys = np.array([p[0] for p in axis_points])
    qs = np.array([p[1] for p in axis_points])
    slope, intercept = np.polyfit(ys, qs, 1)
    return slope, intercept


def _row_medians(img_array, x_start=350):
    """Return per-row median R, G, B arrays across the chart area (skip axis strip)."""
    chart = img_array[:, x_start:, :]
    r = np.median(chart[:, :, 0], axis=1)
    g = np.median(chart[:, :, 1], axis=1)
    b = np.median(chart[:, :, 2], axis=1)
    return r, g, b


def extract_thresholds_from_image(img_url):
    """
    Download threshold chart image and extract 5 category thresholds from
    the coloured background bands using image analysis.
    Returns {category: m3s} or None.
    """
    if not (PIL_AVAILABLE and EASYOCR_AVAILABLE):
        print(f"    Image analysis unavailable (pip install Pillow easyocr). URL: {img_url}")
        return None

    print(f"    Downloading image: {img_url}")
    resp = requests.get(img_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    arr = np.array(img)
    h, w = arr.shape[:2]

    # 1. Build discharge ↔ pixel-y linear scale from OCR'd y-axis
    scale = _build_axis_scale(arr)
    if scale is None:
        print("    Could not read y-axis labels — skipping image threshold extraction")
        return None

    slope, intercept = scale
    def px_to_q(y):
        return slope * y + intercept

    # 2. Row-median colours in the chart body
    row_r, row_g, row_b = _row_medians(arr)
    sat = (np.maximum(np.maximum(row_r, row_g), row_b)
           - np.minimum(np.minimum(row_r, row_g), row_b))

    # 3. Coloured region = where saturation > 12 across chart width
    colored = sat > 12
    colored_ys = np.where(colored)[0]
    if len(colored_ys) == 0:
        print("    No coloured band region found in image")
        return None

    band_top = int(colored_ys[0])
    band_bottom = int(colored_ys[-1])
    print(f"    Coloured region: y={band_top}–{band_bottom}  "
          f"({px_to_q(band_top):.0f}–{px_to_q(band_bottom):.0f} m³/s)")

    # threshold_1 = bottom of the coloured region (where category 1 begins)
    threshold_1 = px_to_q(band_bottom)

    # 4. Find colour transition rows within the coloured region
    # Use R-B (warm/cool) and G channels as colour signature.
    rb = row_r - row_b   # purple → negative, yellow → ~+23, red → ~+62
    g_ch = row_g

    # Detect large jumps in R-B between consecutive coloured rows
    transitions = []
    prev_rb = rb[band_top]
    prev_g = g_ch[band_top]
    for y in range(band_top + 1, band_bottom + 1):
        if not colored[y]:
            continue
        d_rb = abs(float(rb[y]) - float(prev_rb))
        d_g  = abs(float(g_ch[y]) - float(prev_g))
        if d_rb > 15 or d_g > 15:
            transitions.append(y)
        prev_rb = rb[y]
        prev_g = g_ch[y]

    # Merge transitions that are close together (within 5 px)
    merged = []
    for t in transitions:
        if merged and t - merged[-1] <= 5:
            merged[-1] = (merged[-1] + t) // 2
        else:
            merged.append(t)

    print(f"    Colour transitions at y: {merged}  "
          f"≈ {[f'{px_to_q(y):.0f}' for y in merged]} m³/s")

    # The 4 inner transitions (between 5 bands) give thresholds 2–5.
    # Keep only the top 4 (highest discharge) if more found.
    inner = sorted(merged)[:4]   # ascending y = descending discharge
    thresholds_raw = [threshold_1] + [px_to_q(y) for y in inner]
    thresholds_raw.sort()

    if len(thresholds_raw) < 5:
        print(f"    Only {len(thresholds_raw)} thresholds found (need 5)")
        return None

    result = {i: round(v, 1) for i, v in enumerate(thresholds_raw[:5], start=1)}
    print(f"    Extracted thresholds: { {k: f'{v:.0f}' for k, v in result.items()} }")
    return result


def try_html_thresholds(html):
    """Try HTML table parse for thresholds. Returns {cat: m3s} or None."""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        thresholds = {}
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            text = " ".join(cells).lower()
            nums = re.findall(r"\b\d{2,7}(?:\.\d+)?\b", " ".join(cells))
            if not nums:
                continue
            if any(k in text for k in ["1.5", "1-2 yr"]) and 1 not in thresholds:
                thresholds[1] = float(nums[-1])
            elif any(k in text for k in ["2-5", "2 yr"]) and 2 not in thresholds:
                thresholds[2] = float(nums[-1])
            elif "5-10" in text and 3 not in thresholds:
                thresholds[3] = float(nums[-1])
            elif any(k in text for k in ["10-20", "10-25"]) and 4 not in thresholds:
                thresholds[4] = float(nums[-1])
            elif any(k in text for k in ["20+", "25+", "50yr"]) and 5 not in thresholds:
                thresholds[5] = float(nums[-1])
        if len(thresholds) >= 4:
            return thresholds
    return None


def get_thresholds(html, page_url):
    """
    Extract flood thresholds: HTML first, then image analysis fallback.
    Returns ({category: m3s}, derived_from_str).
    """
    thresholds = try_html_thresholds(html)
    if thresholds and len(thresholds) >= 4:
        print("    Thresholds: html_parse")
        return thresholds, "html_parse"

    print("    HTML parse found no thresholds — trying image analysis")
    img_url = find_threshold_image_url(html, page_url)
    if img_url:
        print(f"    Image: {img_url}")
        thresholds = extract_thresholds_from_image(img_url)
        if thresholds and len(thresholds) >= 4:
            return thresholds, "image_ocr"
    else:
        print("    No threshold image found on page")

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
            VALUES (%(sid)s, %(cat)s, %(label)s, %(val)s, %(from)s)
            ON CONFLICT (station_id, category) DO UPDATE
                SET discharge_threshold_m3s = EXCLUDED.discharge_threshold_m3s,
                    derived_from            = EXCLUDED.derived_from
            """,
            {
                "sid": station_uuid,
                "cat": cat,
                "label": RETURN_PERIOD_LABELS.get(cat, f"category_{cat}"),
                "val": value,
                "from": derived_from,
            },
        )


def insert_discharge_ts(cur, station_uuid, source_id, ts_rows):
    for row in ts_rows:
        cur.execute(
            """
            INSERT INTO discharge_ts
                (station_id, obs_date, discharge_m3s, discharge_anomaly_pct,
                 threshold_exceeded_category, qc_flag, source_id)
            VALUES
                (%(sid)s, %(date)s, %(dis)s, NULL, %(cat)s, %(qc)s, %(src)s)
            ON CONFLICT (station_id, obs_date) DO NOTHING
            """,
            {
                "sid": station_uuid,
                "date": row["obs_date"],
                "dis": row["discharge_m3s"],
                "cat": row["threshold_exceeded_category"],
                "qc": row["qc_flag"],
                "src": source_id,
            },
        )


def insert_flood_events(cur, station_uuid, events):
    for ev in events:
        cur.execute(
            """
            INSERT INTO flood_events
                (event_id, station_id, flood_start, flood_end, duration_days,
                 peak_discharge_m3s, peak_date, max_category, detection_method)
            VALUES
                (%(eid)s, %(sid)s, %(start)s, %(end)s, %(dur)s,
                 %(peak_q)s, %(peak_d)s, %(max_cat)s, 'threshold_exceedance')
            ON CONFLICT (event_id) DO NOTHING
            """,
            {
                "eid": ev["event_id"],
                "sid": station_uuid,
                "start": ev["flood_start"],
                "end": ev["flood_end"],
                "dur": ev["duration_days"],
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

def process_station(rec, dry_run, conn, source_id):
    """
    Run full pipeline for one station.
    Returns (ts_count, event_count, thresholds_dict, error_or_None).
    """
    sid = rec["station_id"]
    river_name = rec.get("river_name") or f"Station {sid}"
    lat, lon = rec.get("lat"), rec.get("lon")

    if lat is None or lon is None:
        return 0, 0, {}, "Missing coordinates"

    try:
        html, final_url = fetch_station_page(sid)
    except requests.RequestException as e:
        return 0, 0, {}, f"Page fetch failed: {e}"

    thresholds, derived_from = get_thresholds(html, final_url)

    csv_url = find_csv_url(html)
    if not csv_url:
        return 0, 0, thresholds, "No discharge CSV URL found on station page"

    print(f"  CSV: {csv_url}")
    try:
        raw_records = fetch_and_parse_csv(csv_url)
    except Exception as e:
        return 0, 0, thresholds, f"CSV fetch/parse failed: {e}"

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
