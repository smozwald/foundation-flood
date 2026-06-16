#!/usr/bin/env python3
"""
ingest_s1_flood_pixels.py — Per-pixel S1 backscatter + flood labels.

For every SUCCESS row in zone_flood_analysis, samples Sentinel-1 VV/VH at each
pixel in pixels_static (for that zone) using the same orbit, dry composite, and
wet scene already chosen by calculate_total_flood.py.

Writes:
  ts_sentinel1   — one 'wet_event' row + one 'dry_composite' row per pixel per event
  pixel_flooded  — flood label (is_flooded) + continuous vv_change_db per pixel per event

Usage:
    python agents/ingest_s1_flood_pixels.py [options]

Options:
    --zone-id TEXT       Process a single zone_id
    --zone-pattern TEXT  POSIX regex matched against zone_id (default: '.*')
    --reprocess          Overwrite existing pixel_flooded rows
    --dry-run            Compute and print counts; do not insert
    --test               First qualifying analysis only; still writes
"""

import argparse
import json
import math
import os
import sys
from datetime import date

import ee
import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv()

CONN_STRING = os.environ.get("SUPABASE_CONN_STRING")
if not CONN_STRING:
    print("ERROR: SUPABASE_CONN_STRING not set.")
    sys.exit(1)

MAX_WET_WINDOW_DAYS = 12
SAMPLE_BATCH = 500

DDL_PIXEL_FLOODED = """
CREATE TABLE IF NOT EXISTS pixel_flooded (
    pixel_id           text    NOT NULL REFERENCES pixels_static(pixel_id),
    flood_event_id     uuid    NOT NULL REFERENCES flood_events(event_id),
    analysis_id        uuid    REFERENCES zone_flood_analysis(analysis_id),
    is_flooded         boolean NOT NULL,
    vv_change_db       double precision,
    wet_obs_date       date,
    source_metadata_id uuid    REFERENCES data_sources(source_id),
    PRIMARY KEY (pixel_id, flood_event_id)
)
"""

S1_SOURCE = {
    'product_name': 'Sentinel-1 GRD — SAR flood pixels',
    'resolution_m': 10.0,
    'methodology_desc': (
        'Sentinel-1 GRD IW VV/VH sampled per pixel via GEE sampleRegions(scale=10). '
        'Wet scene: closest S1 acquisition to peak discharge date within +12 days. '
        'Dry composite: median S1 over dry window (Feb 15 – May 15 preceding flood year). '
        'Flood label: is_flooded = (VV_wet_db - VV_dry_db) < otsu_thresh_db where '
        'otsu_thresh_db is read from zone_flood_analysis (not recomputed).'
    ),
}


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(DDL_PIXEL_FLOODED)
    conn.commit()


def ensure_data_source(conn):
    today = date.today().isoformat()
    src = dict(S1_SOURCE)
    src['version_tag'] = f'ingest:{today}'
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO data_sources
                (product_name, version_tag, resolution_m, methodology_desc)
            VALUES
                (%(product_name)s, %(version_tag)s, %(resolution_m)s, %(methodology_desc)s)
            ON CONFLICT (product_name) DO UPDATE
                SET version_tag      = EXCLUDED.version_tag,
                    resolution_m     = EXCLUDED.resolution_m,
                    methodology_desc = EXCLUDED.methodology_desc
            RETURNING source_id::text
        """, src)
        return cur.fetchone()[0]


def load_analysis_pairs(conn, zone_pattern=None, zone_id=None):
    """Return SUCCESS analysis rows that still need pixel-level ingestion."""
    if zone_id:
        clause = "sz.zone_id = %(filter)s"
        param = zone_id
    else:
        clause = "sz.zone_id ~ %(filter)s"
        param = zone_pattern or '.*'

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                zfa.analysis_id::text,
                zfa.zone_id,
                zfa.flood_event_id::text,
                zfa.wet_scene_date,
                zfa.dry_start,
                zfa.dry_end,
                zfa.otsu_thresh_db,
                zfa.peak_discharge_date,
                zfa.flood_start,
                ST_AsGeoJSON(sz.geom) AS zone_geom_json,
                sz.station_id::text
            FROM zone_flood_analysis zfa
            JOIN study_zones sz ON sz.zone_id = zfa.zone_id
            WHERE zfa.status = 'SUCCESS'
              AND {clause}
            ORDER BY zfa.zone_id, zfa.flood_start
        """, {'filter': param})
        rows = cur.fetchall()

    keys = ['analysis_id', 'zone_id', 'flood_event_id', 'wet_scene_date',
            'dry_start', 'dry_end', 'otsu_thresh_db', 'peak_discharge_date',
            'flood_start', 'zone_geom_json', 'station_id']
    return [dict(zip(keys, r)) for r in rows]


def already_ingested_events(conn, zone_id):
    """Return set of flood_event_id strings fully ingested for this zone."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT pf.flood_event_id::text
            FROM pixel_flooded pf
            JOIN pixels_static ps ON ps.pixel_id = pf.pixel_id
            WHERE ps.zone_id = %(zone_id)s
        """, {'zone_id': zone_id})
        return {r[0] for r in cur.fetchall()}


def load_pixels(conn, zone_id):
    """Return list of (pixel_id, lon, lat) for a zone."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pixel_id, ST_X(geom) AS lon, ST_Y(geom) AS lat
            FROM pixels_static
            WHERE zone_id = %(zone_id)s AND geom IS NOT NULL
        """, {'zone_id': zone_id})
        return cur.fetchall()


# ── GEE helpers ────────────────────────────────────────────────────────────────

def _to_linear_dual(img):
    lin = ee.Image(10.0).pow(img.select(['VV', 'VH']).divide(10.0)).rename(['VV', 'VH'])
    return lin.addBands(img.select('angle')).copyProperties(img, ['system:time_start'])


def get_best_orbit(zone_geom):
    from collections import Counter
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
    viable = {k: v for k, v in combo_count.items() if v >= 10} or combo_count
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


def build_s1_dual(zone_geom, pass_dir, orbit):
    """S1 collection with VV, VH, angle in linear scale."""
    return (
        ee.ImageCollection('COPERNICUS/S1_GRD')
        .filterBounds(zone_geom)
        .filter(ee.Filter.eq('instrumentMode', 'IW'))
        .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
        .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VH'))
        .filter(ee.Filter.eq('orbitProperties_pass', pass_dir))
        .filter(ee.Filter.eq('relativeOrbitNumber_start', orbit))
        .select(['VV', 'VH', 'angle'])
        .map(_to_linear_dual)
    )


def build_dry_composite(s1_col, dry_start, dry_end):
    """Median dry composite, VV+VH in dB."""
    dry_col = s1_col.filter(ee.Filter.date(
        dry_start.strftime('%Y-%m-%d'),
        (dry_end + pd.Timedelta(days=1)).strftime('%Y-%m-%d'),
    ))
    vv = dry_col.select('VV').median().log10().multiply(10.0).rename('VV')
    vh = dry_col.select('VH').median().log10().multiply(10.0).rename('VH')
    return vv.addBands(vh)


def build_wet_scene(s1_col, peak_date):
    """Closest S1 scene to peak_date within +MAX_WET_WINDOW_DAYS, VV+VH+angle in dB."""
    window_end = peak_date + pd.Timedelta(days=MAX_WET_WINDOW_DAYS)
    wet_col = s1_col.filter(ee.Filter.date(
        peak_date.strftime('%Y-%m-%d'),
        (window_end + pd.Timedelta(days=1)).strftime('%Y-%m-%d'),
    ))
    if int(wet_col.size().getInfo()) == 0:
        return None, None
    peak_ms = peak_date.timestamp() * 1000
    closest = ee.Image(
        wet_col
        .map(lambda img: img.set('dt', img.date().millis().subtract(peak_ms).abs()))
        .sort('dt')
        .first()
    )
    scene_date = pd.Timestamp(closest.date().millis().getInfo(), unit='ms').date()
    vv = closest.select('VV').log10().multiply(10.0).rename('VV')
    vh = closest.select('VH').log10().multiply(10.0).rename('VH')
    angle = closest.select('angle')
    return vv.addBands(vh).addBands(angle), scene_date


def sample_image(image, pixels, bands):
    """
    Sample image at pixel centroids in batches of SAMPLE_BATCH.
    Returns dict: pixel_id -> {band: value, ...}
    """
    results = {}
    for start in range(0, len(pixels), SAMPLE_BATCH):
        batch = pixels[start:start + SAMPLE_BATCH]
        features = [
            ee.Feature(ee.Geometry.Point([lon, lat]), {'pixel_id': pid})
            for pid, lon, lat in batch
        ]
        fc = ee.FeatureCollection(features)
        sampled = image.select(bands).sampleRegions(
            collection=fc, properties=['pixel_id'], scale=10, tileScale=4
        )
        for feat in sampled.getInfo().get('features', []):
            props = feat['properties']
            pid = props['pixel_id']
            results[pid] = {b: props.get(b) for b in bands}
    return results


# ── DB writes ──────────────────────────────────────────────────────────────────

def write_ts_rows(conn, pixels, wet_vals, dry_vals,
                  wet_date, dry_date, event_id, analysis_id, pass_dir, source_id):
    with conn.cursor() as cur:
        for pid, _lon, _lat in pixels:
            # wet event row
            w = wet_vals.get(pid, {})
            cur.execute("""
                INSERT INTO ts_sentinel1 (pixel_id, obs_date, vv, vh, incidence_angle, metadata)
                VALUES (%(pid)s, %(obs_date)s, %(vv)s, %(vh)s, %(angle)s, %(meta)s::jsonb)
                ON CONFLICT (pixel_id, obs_date) DO NOTHING
            """, {
                'pid': pid,
                'obs_date': wet_date,
                'vv': w.get('VV'),
                'vh': w.get('VH'),
                'angle': w.get('angle'),
                'meta': json.dumps({
                    'flood_event_id': event_id,
                    'analysis_id': analysis_id,
                    'observation_type': 'wet_event',
                    'orbit_direction': pass_dir,
                }),
            })
            # dry composite row
            d = dry_vals.get(pid, {})
            cur.execute("""
                INSERT INTO ts_sentinel1 (pixel_id, obs_date, vv, vh, incidence_angle, metadata)
                VALUES (%(pid)s, %(obs_date)s, %(vv)s, %(vh)s, %(angle)s, %(meta)s::jsonb)
                ON CONFLICT (pixel_id, obs_date) DO NOTHING
            """, {
                'pid': pid,
                'obs_date': dry_date,
                'vv': d.get('VV'),
                'vh': d.get('VH'),
                'angle': None,
                'meta': json.dumps({
                    'flood_event_id': event_id,
                    'analysis_id': analysis_id,
                    'observation_type': 'dry_composite',
                    'orbit_direction': pass_dir,
                }),
            })
    conn.commit()


def write_flood_labels(conn, pixels, wet_vals, dry_vals, otsu_thresh,
                       event_id, analysis_id, wet_date, source_id, reprocess):
    conflict_action = (
        "DO UPDATE SET is_flooded = EXCLUDED.is_flooded, "
        "vv_change_db = EXCLUDED.vv_change_db, wet_obs_date = EXCLUDED.wet_obs_date"
        if reprocess else "DO NOTHING"
    )
    with conn.cursor() as cur:
        for pid, _lon, _lat in pixels:
            vv_wet = (wet_vals.get(pid) or {}).get('VV')
            vv_dry = (dry_vals.get(pid) or {}).get('VV')
            if vv_wet is None or vv_dry is None:
                continue
            change = vv_wet - vv_dry
            is_flooded = change < otsu_thresh
            cur.execute(f"""
                INSERT INTO pixel_flooded
                    (pixel_id, flood_event_id, analysis_id, is_flooded,
                     vv_change_db, wet_obs_date, source_metadata_id)
                VALUES
                    (%(pid)s, %(event_id)s::uuid, %(analysis_id)s::uuid,
                     %(is_flooded)s, %(change)s, %(wet_date)s, %(source_id)s::uuid)
                ON CONFLICT (pixel_id, flood_event_id) {conflict_action}
            """, {
                'pid': pid,
                'event_id': event_id,
                'analysis_id': analysis_id,
                'is_flooded': is_flooded,
                'change': round(change, 4),
                'wet_date': wet_date,
                'source_id': source_id,
            })
    conn.commit()


# ── Main processing ────────────────────────────────────────────────────────────

def process_analysis(analysis, pixels, source_id, dry_run, reprocess):
    import json as _json
    from shapely.geometry import shape

    zone_id    = analysis['zone_id']
    event_id   = analysis['flood_event_id']
    analysis_id = analysis['analysis_id']
    otsu_thresh = analysis['otsu_thresh_db']
    dry_start   = pd.Timestamp(analysis['dry_start'])
    dry_end     = pd.Timestamp(analysis['dry_end'])
    peak_date   = pd.Timestamp(
        analysis['peak_discharge_date'] or analysis['flood_start']
    )

    zone_geom = ee.Geometry(json.loads(analysis['zone_geom_json']))

    print(f"  Getting orbit ...", end='', flush=True)
    (pass_dir, orbit), _ = get_best_orbit(zone_geom)
    if pass_dir is None:
        print(" no S1 orbit found — skipped")
        return 0
    print(f" {pass_dir} orbit {orbit}")

    s1_col = build_s1_dual(zone_geom, pass_dir, orbit)

    print(f"  Building dry composite ({dry_start.date()} – {dry_end.date()}) ...",
          end='', flush=True)
    dry_img = build_dry_composite(s1_col, dry_start, dry_end)
    print(" done")

    print(f"  Finding wet scene (peak {peak_date.date()}) ...", end='', flush=True)
    wet_img, wet_date = build_wet_scene(s1_col, peak_date)
    if wet_img is None:
        print(" no scene found — skipped")
        return 0
    print(f" {wet_date}")

    if dry_run:
        print(f"  DRY-RUN: would sample {len(pixels)} pixels")
        return len(pixels)

    print(f"  Sampling {len(pixels)} pixels in batches of {SAMPLE_BATCH} ...")
    n_batches = math.ceil(len(pixels) / SAMPLE_BATCH)

    print(f"    wet scene ...", end='', flush=True)
    wet_vals = sample_image(wet_img, pixels, ['VV', 'VH', 'angle'])
    print(f" {len(wet_vals)} hits")

    print(f"    dry composite ...", end='', flush=True)
    dry_vals = sample_image(dry_img, pixels, ['VV', 'VH'])
    print(f" {len(dry_vals)} hits")

    with psycopg2.connect(CONN_STRING) as conn:
        write_ts_rows(conn, pixels, wet_vals, dry_vals,
                      wet_date, dry_end.date(), event_id, analysis_id, pass_dir, source_id)
        write_flood_labels(conn, pixels, wet_vals, dry_vals,
                           otsu_thresh, event_id, analysis_id, wet_date, source_id, reprocess)

    flooded = sum(
        1 for pid, _, _ in pixels
        if (wet_vals.get(pid) or {}).get('VV') is not None
        and (dry_vals.get(pid) or {}).get('VV') is not None
        and ((wet_vals[pid]['VV'] - dry_vals[pid]['VV']) < otsu_thresh)
    )
    print(f"  Written: {len(wet_vals)} ts rows, {flooded}/{len(pixels)} flooded")
    return len(pixels)


def main():
    parser = argparse.ArgumentParser(
        description="Sample per-pixel S1 flood values into ts_sentinel1 and pixel_flooded."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--zone-id',      help='Single zone_id to process')
    group.add_argument('--zone-pattern', default='.*',
                       help="POSIX regex on zone_id (default: '.*')")
    parser.add_argument('--reprocess',  action='store_true',
                        help='Overwrite existing pixel_flooded rows')
    parser.add_argument('--dry-run',    action='store_true',
                        help='Print counts only; do not insert')
    parser.add_argument('--test',       action='store_true',
                        help='Process first qualifying analysis only')
    args = parser.parse_args()

    print(f"Zone filter : {args.zone_id or args.zone_pattern}")
    print(f"Reprocess   : {args.reprocess}")
    print(f"Dry-run     : {args.dry_run}")
    print(f"Test        : {args.test}\n")

    key_path = os.path.join(os.path.dirname(__file__), '..', 'gcp-key.json')
    if os.path.exists(key_path):
        import json
        with open(key_path) as f:
            key_data = json.load(f)
        creds = ee.ServiceAccountCredentials(key_data['client_email'], key_path)
        ee.Initialize(creds, project='foundation-flood')
    else:
        ee.Initialize(project='foundation-flood')

    try:
        with psycopg2.connect(CONN_STRING) as conn:
            if not args.dry_run:
                ensure_tables(conn)
            source_id = ensure_data_source(conn) if not args.dry_run else None

            analyses = load_analysis_pairs(
                conn,
                zone_pattern=args.zone_pattern if not args.zone_id else None,
                zone_id=args.zone_id,
            )
            print(f"Analyses (SUCCESS): {len(analyses)}")

            if not args.reprocess:
                # group by zone, skip fully ingested events
                skip_by_zone = {}
                for a in analyses:
                    zid = a['zone_id']
                    if zid not in skip_by_zone:
                        skip_by_zone[zid] = already_ingested_events(conn, zid)
                before = len(analyses)
                analyses = [
                    a for a in analyses
                    if a['flood_event_id'] not in skip_by_zone.get(a['zone_id'], set())
                ]
                print(f"Skipped (already ingested): {before - len(analyses)}")

            if args.test and len(analyses) > 1:
                print(f"TEST mode: limiting to first of {len(analyses)} analyses.\n")
                analyses = analyses[:1]

            # cache pixels per zone to avoid repeated DB queries
            pixel_cache = {}

        if not analyses:
            print("Nothing to process.")
            sys.exit(0)

        processed, skipped, errors = 0, 0, 0

        for i, analysis in enumerate(analyses):
            zone_id  = analysis['zone_id']
            event_id = analysis['flood_event_id']
            print(f"\n[{i+1}/{len(analyses)}] {zone_id} :: {event_id[:8]}...")

            try:
                if zone_id not in pixel_cache:
                    with psycopg2.connect(CONN_STRING) as conn:
                        pixel_cache[zone_id] = load_pixels(conn, zone_id)
                pixels = pixel_cache[zone_id]

                if not pixels:
                    print(f"  No pixels for zone {zone_id} — skipped")
                    skipped += 1
                    continue

                print(f"  Pixels: {len(pixels)}")
                n = process_analysis(analysis, pixels, source_id,
                                     args.dry_run, args.reprocess)
                if n:
                    processed += 1
                else:
                    skipped += 1

            except Exception as exc:
                print(f"  ERROR: {exc}")
                errors += 1

        print(f"\nDone. processed={processed}  skipped={skipped}  errors={errors}")

    except (psycopg2.Error, ValueError) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    sys.exit(0 if errors == 0 else 1)


if __name__ == '__main__':
    main()
