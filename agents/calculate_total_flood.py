#!/usr/bin/env python3
"""
calculate_total_flood.py — SAR OTSU flood mapping per study zone and flood event.

Implements the Clement et al. (2018) Otsu method from the notebook Cell 10.
Creates zone_flood_analysis in the database if absent, then processes each
zone+event pair, writing SUCCESS or FAIL with Sentinel scene references and
flood extent metrics.

Usage:
    python agents/calculate_total_flood.py [--zone-set NAME] [--rivers R1,R2,...] [--test]
"""

import argparse
import json
import os
import sys
import time
from collections import Counter

import ee
import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv()

CONN_STRING = os.environ.get("SUPABASE_CONN_STRING")
if not CONN_STRING:
    print("ERROR: SUPABASE_CONN_STRING not set.")
    sys.exit(1)

# ── Algorithm config ───────────────────────────────────────────────────────────
DRY_START_MMDD        = (2, 15)
DRY_END_MMDD          = (5, 15)
DRY_N_WEEKS           = 4
MAX_WET_WINDOW_DAYS   = 12
OTSU_BUCKETS          = 256
OTSU_MAX_THRESHOLD    = -0.5

# ──────────────────────────────────────────────────────────────────────────────

DDL_ZONE_FLOOD_ANALYSIS = """
CREATE TABLE IF NOT EXISTS zone_flood_analysis (
    analysis_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    zone_id           text NOT NULL REFERENCES study_zones(zone_id),
    flood_event_id    uuid REFERENCES flood_events(event_id),
    flood_start       date,
    flood_end         date,
    max_category      smallint,
    status            text NOT NULL CHECK (status IN ('SUCCESS', 'FAIL')),
    fail_reason       text,
    otsu_thresh_db    float,
    otsu_valley_ratio float,
    flooded_agri_px   integer,
    total_agri_px     integer,
    flooded_agri_pct  float,
    dry_start         date,
    dry_end           date,
    dry_scenes_json   jsonb,
    wet_scene_date        date,
    wet_method            text,
    peak_discharge_date   date,
    peak_discharge_m3s    float,
    processed_at          timestamptz DEFAULT now(),
    UNIQUE (zone_id, flood_event_id)
)
"""


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(DDL_ZONE_FLOOD_ANALYSIS)
        cur.execute("""
            ALTER TABLE zone_flood_analysis
                ADD COLUMN IF NOT EXISTS peak_discharge_date date,
                ADD COLUMN IF NOT EXISTS peak_discharge_m3s  float
        """)
    conn.commit()


# ── GEE helpers ────────────────────────────────────────────────────────────────

def to_linear(img):
    return (ee.Image(10.0)
              .pow(img.divide(10.0))
              .rename('VV')
              .copyProperties(img, ['system:time_start']))


def otsu_threshold(histogram_list):
    arr    = np.array(histogram_list, dtype=float)
    means  = arr[:, 0]
    counts = arr[:, 1]
    total  = counts.sum()
    if total == 0:
        return float(means[0]), 1.0

    wsum  = (means * counts).sum()
    gmean = wsum / total
    best_bss, best_idx = -1.0, 0
    a_count, a_sum = 0.0, 0.0

    for i in range(1, len(means)):
        a_count += counts[i - 1]
        a_sum   += means[i - 1] * counts[i - 1]
        b_count  = total - a_count
        if a_count == 0 or b_count == 0:
            continue
        a_mean = a_sum / a_count
        b_mean = (wsum - a_sum) / b_count
        bss    = (a_count * (a_mean - gmean) ** 2
                  + b_count * (b_mean - gmean) ** 2)
        if bss > best_bss:
            best_bss = bss
            best_idx = i - 1

    best_thresh  = float(means[best_idx])
    left_peak    = float(counts[:best_idx].max())   if best_idx > 0              else 0.0
    right_peak   = float(counts[best_idx+1:].max()) if best_idx < len(counts)-1  else 0.0
    peak         = max(left_peak, right_peak, 1.0)
    valley_ratio = float(counts[best_idx]) / peak
    return best_thresh, valley_ratio


def get_best_orbit(zone_geom):
    s1_all = (
        ee.ImageCollection('COPERNICUS/S1_GRD')
        .filterBounds(zone_geom)
        .filter(ee.Filter.eq('instrumentMode', 'IW'))
        .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
        .select('VV', 'angle')
    )
    passes = s1_all.aggregate_array('orbitProperties_pass').getInfo()
    orbits = s1_all.aggregate_array('relativeOrbitNumber_start').getInfo()
    if not passes:
        return None, None

    combo_count = Counter(zip(passes, orbits))
    viable      = {k: v for k, v in combo_count.items() if v >= 10} or combo_count
    best_combo, best_angle = None, 999.0

    for (pass_dir, orbit) in viable:
        angle = (
            s1_all
            .filter(ee.Filter.eq('orbitProperties_pass', pass_dir))
            .filter(ee.Filter.eq('relativeOrbitNumber_start', orbit))
            .select('angle').mean()
            .reduceRegion(reducer=ee.Reducer.mean(), geometry=zone_geom,
                          scale=100, maxPixels=1e7)
            .getInfo().get('angle', 999.0)
        )
        if angle < best_angle:
            best_angle, best_combo = angle, (pass_dir, orbit)
    return best_combo, best_angle


def build_s1(zone_geom, pass_dir, orbit):
    return (
        ee.ImageCollection('COPERNICUS/S1_GRD')
        .filterBounds(zone_geom)
        .filter(ee.Filter.eq('instrumentMode', 'IW'))
        .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
        .filter(ee.Filter.eq('orbitProperties_pass', pass_dir))
        .filter(ee.Filter.eq('relativeOrbitNumber_start', orbit))
        .select('VV')
        .map(to_linear)
    )


def get_preseasonal_dry_window(flood_start, station_id):
    year      = flood_start.year
    dry_start = pd.Timestamp(year=year, month=DRY_START_MMDD[0], day=DRY_START_MMDD[1])
    dry_end   = pd.Timestamp(year=year, month=DRY_END_MMDD[0],   day=DRY_END_MMDD[1])
    if flood_start <= dry_end:
        dry_start = dry_start.replace(year=year - 1)
        dry_end   = dry_end.replace(year=year - 1)

    with psycopg2.connect(CONN_STRING) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    DATE_TRUNC('week', obs_date)::date         AS week_start,
                    AVG(discharge_m3s)                         AS week_mean,
                    ARRAY_AGG(obs_date::text ORDER BY obs_date) AS dates
                FROM discharge_ts
                WHERE station_id = %(sid)s::uuid
                  AND obs_date BETWEEN %(ds)s AND %(de)s
                  AND discharge_m3s IS NOT NULL
                  AND qc_flag IS DISTINCT FROM 'bad'
                GROUP BY DATE_TRUNC('week', obs_date)
                ORDER BY week_mean ASC
                LIMIT %(n_weeks)s
            """, {'sid': station_id, 'ds': dry_start.strftime('%Y-%m-%d'),
                  'de': dry_end.strftime('%Y-%m-%d'), 'n_weeks': DRY_N_WEEKS})
            rows = cur.fetchall()

    if not rows:
        return None
    low_q_dates = sorted(d for _, _, dates in rows for d in dates)
    return dry_start, dry_end, low_q_dates


def get_peak_discharge_date(station_id, flood_start, flood_end):
    with psycopg2.connect(CONN_STRING) as conn:
        df = pd.read_sql("""
            SELECT obs_date, discharge_m3s
            FROM discharge_ts
            WHERE station_id = %(sid)s::uuid
              AND obs_date BETWEEN %(ds)s AND %(de)s
              AND discharge_m3s IS NOT NULL
              AND qc_flag IS DISTINCT FROM 'bad'
            ORDER BY discharge_m3s DESC
            LIMIT 1
        """, conn, params={
            'sid': station_id,
            'ds':  flood_start.strftime('%Y-%m-%d'),
            'de':  flood_end.strftime('%Y-%m-%d'),
        })
    if df.empty:
        return None, None
    row = df.iloc[0]
    return pd.Timestamp(row['obs_date']), float(row['discharge_m3s'])


def build_dry_composite(s1_col, dry_start, dry_end):
    dry_col = s1_col.filter(ee.Filter.date(
        dry_start.strftime('%Y-%m-%d'),
        (dry_end + pd.Timedelta(days=1)).strftime('%Y-%m-%d'),
    ))
    return dry_col.median().log10().multiply(10.0).rename('VV')


def build_wet_scene(s1_col, peak_date):
    window_start = peak_date
    window_end   = peak_date + pd.Timedelta(days=MAX_WET_WINDOW_DAYS)
    wet_col = s1_col.filter(ee.Filter.date(
        window_start.strftime('%Y-%m-%d'),
        (window_end + pd.Timedelta(days=1)).strftime('%Y-%m-%d'),
    ))
    if int(wet_col.size().getInfo()) == 0:
        return None
    peak_ms = peak_date.timestamp() * 1000
    closest = ee.Image(
        wet_col
        .map(lambda img: img.set('dt', img.date().millis().subtract(peak_ms).abs()))
        .sort('dt')
        .first()
    )
    scene_date_ms = closest.date().millis().getInfo()
    scene_date    = pd.Timestamp(scene_date_ms, unit='ms').date()
    wet_db = closest.log10().multiply(10.0).rename('VV')
    return wet_db, closest, scene_date


def _get_histogram(change_img, geom, perm_water_mask):
    raw_dict = (
        change_img.clip(geom).updateMask(perm_water_mask)
        .reduceRegion(
            reducer=ee.Reducer.autoHistogram(maxBuckets=OTSU_BUCKETS, cumulative=False),
            geometry=geom, scale=100, maxPixels=1e10, bestEffort=True,
        )
        .getInfo()
    )
    raw = raw_dict.get('change') if raw_dict else None
    return raw if (raw is not None and len(raw) >= 2) else None


def compute_flood_mask(wet_db, dry_db, zone_geom, best_scene, perm_water_mask):
    change   = wet_db.subtract(dry_db).rename('change')
    hist_raw = _get_histogram(change, best_scene.geometry(), perm_water_mask)
    if hist_raw is None:
        hist_raw = _get_histogram(change, zone_geom, perm_water_mask)
    if hist_raw is None:
        return None, None, None, None
    thresh, valley_ratio = otsu_threshold(hist_raw)
    if thresh > OTSU_MAX_THRESHOLD:
        return None, change, thresh, valley_ratio
    flood_mask = change.lt(thresh).clip(zone_geom)
    return flood_mask, change, thresh, valley_ratio


def count_flooded_agri(flood_mask, zone_geom, agri_mask):
    agri         = agri_mask.clip(zone_geom)
    flooded_agri = flood_mask.And(agri)
    result = ee.Dictionary({
        'total':   agri.reduceRegion(
            reducer=ee.Reducer.sum(), geometry=zone_geom, scale=10, maxPixels=1e9
        ).get('Map'),
        'flooded': flooded_agri.rename('flooded').reduceRegion(
            reducer=ee.Reducer.sum(), geometry=zone_geom, scale=10, maxPixels=1e9
        ).get('flooded'),
    }).getInfo()
    return float(result.get('flooded') or 0), float(result.get('total') or 0)


def write_result(conn, record):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO zone_flood_analysis (
                zone_id, flood_event_id, flood_start, flood_end, max_category,
                status, fail_reason, otsu_thresh_db, otsu_valley_ratio,
                flooded_agri_px, total_agri_px, flooded_agri_pct,
                dry_start, dry_end, dry_scenes_json, wet_scene_date, wet_method,
                peak_discharge_date, peak_discharge_m3s
            ) VALUES (
                %(zone_id)s, %(flood_event_id)s, %(flood_start)s, %(flood_end)s,
                %(max_category)s, %(status)s, %(fail_reason)s, %(otsu_thresh_db)s,
                %(otsu_valley_ratio)s, %(flooded_agri_px)s, %(total_agri_px)s,
                %(flooded_agri_pct)s, %(dry_start)s, %(dry_end)s,
                %(dry_scenes_json)s::jsonb, %(wet_scene_date)s, %(wet_method)s,
                %(peak_discharge_date)s, %(peak_discharge_m3s)s
            )
            ON CONFLICT (zone_id, flood_event_id) DO UPDATE
                SET status            = EXCLUDED.status,
                    fail_reason       = EXCLUDED.fail_reason,
                    otsu_thresh_db    = EXCLUDED.otsu_thresh_db,
                    otsu_valley_ratio = EXCLUDED.otsu_valley_ratio,
                    flooded_agri_px   = EXCLUDED.flooded_agri_px,
                    total_agri_px     = EXCLUDED.total_agri_px,
                    flooded_agri_pct  = EXCLUDED.flooded_agri_pct,
                    dry_start         = EXCLUDED.dry_start,
                    dry_end           = EXCLUDED.dry_end,
                    dry_scenes_json   = EXCLUDED.dry_scenes_json,
                    wet_scene_date        = EXCLUDED.wet_scene_date,
                    wet_method            = EXCLUDED.wet_method,
                    peak_discharge_date   = EXCLUDED.peak_discharge_date,
                    peak_discharge_m3s    = EXCLUDED.peak_discharge_m3s,
                    processed_at          = now()
                WHERE zone_flood_analysis.status = 'FAIL'
        """, record)
    conn.commit()


def main():
    parser = argparse.ArgumentParser(
        description="SAR OTSU flood mapping — writes to zone_flood_analysis."
    )
    parser.add_argument("--zone-set", default="initial",
                        help="Zone set to process (default: initial)")
    parser.add_argument("--rivers",
                        default="Indus River,Chenab River,River_Unknown",
                        help="Comma-separated river names to include")
    parser.add_argument("--test", action="store_true",
                        help="Process only the first zone+event then exit")
    args = parser.parse_args()

    study_rivers = [r.strip() for r in args.rivers.split(",")]
    print(f"Rivers   : {study_rivers}")
    print(f"Zone set : '{args.zone_set}'")
    print(f"TEST     : {args.test}")

    ee.Initialize(project='foundation-flood')

    jrc_water       = ee.Image('JRC/GSW1_4/GlobalSurfaceWater')
    perm_water_mask = jrc_water.select('seasonality').gte(10).Not()
    worldcover      = ee.ImageCollection('ESA/WorldCover/v200').first()
    agri_mask       = worldcover.eq(40)

    try:
        with psycopg2.connect(CONN_STRING) as conn:
            ensure_table(conn)

            existing_ok = set(
                pd.read_sql(
                    "SELECT zone_id, flood_event_id::text FROM zone_flood_analysis"
                    " WHERE status = 'SUCCESS'",
                    conn,
                ).itertuples(index=False, name=None)
            )
            print(f"Already SUCCESS: {len(existing_ok)} pairs — will skip these")

            station_ids_df = pd.read_sql("""
                SELECT DISTINCT ds.station_id::text
                FROM discharge_stations ds
                JOIN rivers r ON r.river_id = ds.river_id
                JOIN study_zones sz ON sz.station_id = ds.station_id
                WHERE r.river_name = ANY(%(rivers)s)
                  AND sz.zone_set  = %(zone_set)s
            """, conn, params={'rivers': study_rivers, 'zone_set': args.zone_set})

            station_ids = station_ids_df['station_id'].tolist()
            print(f"Stations : {len(station_ids)}")

            flood_events = pd.read_sql("""
                SELECT fe.event_id::text, fe.station_id::text,
                       fe.flood_start, fe.flood_end, fe.max_category
                FROM flood_events fe
                JOIN discharge_stations ds USING (station_id)
                JOIN rivers r ON r.river_id = ds.river_id
                WHERE fe.station_id = ANY(%(ids)s::uuid[])
                  AND EXTRACT(MONTH FROM fe.flood_start) BETWEEN 7 AND 9
                  AND fe.detection_method = 'threshold_exceedance_7d'
                  AND r.river_name = ANY(%(rivers)s)
                ORDER BY fe.flood_start
            """, conn, params={'ids': station_ids, 'rivers': study_rivers})

            zones_df = pd.read_sql("""
                SELECT sz.zone_id, sz.station_id::text, ds.station_name,
                       r.river_name,
                       ST_AsGeoJSON(ST_Transform(sz.geom, 4326)) AS geom_json
                FROM study_zones sz
                JOIN discharge_stations ds USING (station_id)
                JOIN rivers r ON r.river_id = ds.river_id
                WHERE sz.zone_set = %(zone_set)s
                  AND sz.station_id = ANY(%(ids)s::uuid[])
                  AND r.river_name = ANY(%(rivers)s)
                ORDER BY ds.station_name
            """, conn, params={'ids': station_ids, 'rivers': study_rivers,
                               'zone_set': args.zone_set})

        for df in [flood_events]:
            df['flood_start'] = pd.to_datetime(df['flood_start'])
            df['flood_end']   = pd.to_datetime(df['flood_end'])

        if args.test:
            zones_df = zones_df.head(1)

        print(f"Zones    : {len(zones_df)}")
        print(f"Events   : {len(flood_events)}\n")

        processed = 0

        for _, zone in zones_df.iterrows():
            zone_geom      = ee.Geometry(json.loads(zone['geom_json']))
            station_floods = flood_events[flood_events['station_id'] == zone['station_id']]

            orbit_result = get_best_orbit(zone_geom)
            if orbit_result == (None, None):
                print(f"  {zone['station_name']}: no S1 orbit — skipping")
                continue

            (pass_dir, orbit), best_angle = orbit_result
            s1_col = build_s1(zone_geom, pass_dir, orbit)

            if station_floods.empty:
                continue

            print(f"\n{zone['river_name']} - {zone['station_name']} ({zone['zone_id']})"
                  f"  |  {pass_dir} rel={orbit}  incidence={best_angle:.1f}°")

            for _, row in station_floods.iterrows():
                if args.test and processed >= 1:
                    break

                flood_start  = row['flood_start']
                flood_end    = row['flood_end']
                max_category = row.get('max_category')
                event_id     = row['event_id']

                if (zone['zone_id'], event_id) in existing_ok:
                    print(f"  {flood_start.date()} → SKIP (already SUCCESS)")
                    continue

                print(f"  {flood_start.date()} "
                      f"cat={int(max_category) if pd.notna(max_category) else '?'} ",
                      end='', flush=True)

                base = {
                    'zone_id':          zone['zone_id'],
                    'flood_event_id':   event_id,
                    'flood_start':      flood_start.date(),
                    'flood_end':        flood_end.date(),
                    'max_category':     int(max_category) if pd.notna(max_category) else None,
                    'otsu_thresh_db':       None, 'otsu_valley_ratio':    None,
                    'flooded_agri_px':      None, 'total_agri_px':        None,
                    'flooded_agri_pct':     None,
                    'dry_start':            None, 'dry_end':              None,
                    'dry_scenes_json':      None, 'wet_scene_date':       None,
                    'wet_method':           None,
                    'peak_discharge_date':  None, 'peak_discharge_m3s':   None,
                }

                peak_date, peak_q = get_peak_discharge_date(
                    zone['station_id'], flood_start, flood_end)
                if peak_date is None:
                    peak_date = flood_start
                base['peak_discharge_date'] = peak_date.date() if peak_date is not None else None
                base['peak_discharge_m3s']  = peak_q

                window = get_preseasonal_dry_window(flood_start, zone['station_id'])
                if window is None:
                    print('→ FAIL: no dry window')
                    with psycopg2.connect(CONN_STRING) as conn:
                        write_result(conn, {**base, 'status': 'FAIL',
                                            'fail_reason': 'no dry window'})
                    continue

                dry_start, dry_end, low_q_dates = window

                t0 = time.time()
                dry_db = build_dry_composite(s1_col, dry_start, dry_end)
                print(f"dry:{time.time()-t0:.1f}s ", end='', flush=True)

                t0 = time.time()
                wet_result = build_wet_scene(s1_col, peak_date)
                print(f"wet:{time.time()-t0:.1f}s ", end='', flush=True)

                if wet_result is None:
                    print(f"→ FAIL: no wet scene ±{MAX_WET_WINDOW_DAYS}d of {peak_date.date()}")
                    with psycopg2.connect(CONN_STRING) as conn:
                        write_result(conn, {**base, 'status': 'FAIL',
                                            'fail_reason': f'no wet scene ±{MAX_WET_WINDOW_DAYS}d',
                                            'dry_start':       dry_start.date(),
                                            'dry_end':         dry_end.date(),
                                            'dry_scenes_json': json.dumps(low_q_dates)})
                    continue

                wet_db, best_scene, wet_scene_date = wet_result

                t0 = time.time()
                mask_result = compute_flood_mask(wet_db, dry_db, zone_geom,
                                                 best_scene, perm_water_mask)
                flood_mask, _, thresh, valley_ratio = mask_result

                if thresh is None:
                    print('→ FAIL: no histogram')
                    with psycopg2.connect(CONN_STRING) as conn:
                        write_result(conn, {**base, 'status': 'FAIL',
                                            'fail_reason':     'no histogram',
                                            'dry_start':       dry_start.date(),
                                            'dry_end':         dry_end.date(),
                                            'dry_scenes_json': json.dumps(low_q_dates),
                                            'wet_scene_date':  wet_scene_date,
                                            'wet_method':      'single scene'})
                    continue

                if flood_mask is None:
                    print(f'→ FAIL: otsu thresh={thresh:.2f} dB valley={valley_ratio:.2f}')
                    with psycopg2.connect(CONN_STRING) as conn:
                        write_result(conn, {**base, 'status': 'FAIL',
                                            'fail_reason':      f'otsu thresh={thresh:.2f} dB '
                                                                f'valley={valley_ratio:.2f}',
                                            'otsu_thresh_db':   thresh,
                                            'otsu_valley_ratio': round(valley_ratio, 3),
                                            'dry_start':        dry_start.date(),
                                            'dry_end':          dry_end.date(),
                                            'dry_scenes_json':  json.dumps(low_q_dates),
                                            'wet_scene_date':   wet_scene_date,
                                            'wet_method':       'single scene'})
                    continue

                print(f"otsu:{time.time()-t0:.1f}s ", end='', flush=True)

                t0 = time.time()
                flooded_n, total_agri = count_flooded_agri(flood_mask, zone_geom, agri_mask)
                pct = (flooded_n / total_agri * 100) if total_agri else float('nan')
                print(f"agri:{time.time()-t0:.1f}s → {pct:.1f}% SUCCESS")

                with psycopg2.connect(CONN_STRING) as conn:
                    write_result(conn, {**base, 'status': 'SUCCESS',
                                        'fail_reason':       None,
                                        'otsu_thresh_db':    thresh,
                                        'otsu_valley_ratio': round(valley_ratio, 3),
                                        'flooded_agri_px':   int(flooded_n),
                                        'total_agri_px':     int(total_agri),
                                        'flooded_agri_pct':  round(pct, 2),
                                        'dry_start':         dry_start.date(),
                                        'dry_end':           dry_end.date(),
                                        'dry_scenes_json':   json.dumps(low_q_dates),
                                        'wet_scene_date':    wet_scene_date,
                                        'wet_method':        'single scene'})
                processed += 1

    except psycopg2.Error as exc:
        print(f"Database error: {exc}")
        sys.exit(1)

    print(f"\nDone: {processed} events processed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
