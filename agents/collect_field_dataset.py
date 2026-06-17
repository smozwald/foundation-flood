#!/usr/bin/env python3
"""
collect_field_dataset.py — Phase 2 Step 2: Field-level modelling dataset.

One row per (FoTW field × flood event):
  - flood_pct    : fraction of pixels in that field labelled is_flooded
  - n_pixels     : pixel count supporting the label (pixel-support guard)
  - features     : per-field static terrain + seasonal GEE metrics (jsonb)

Label source  : pixel_flooded.is_flooded → aggregated to FoTW field via
                spatial join (pixels_static.geom ∩ FoTW field polygon).
Static terrain: mean/std over pixels in field for continuous vars; mode for
                categorical (landcover, soil_hsg).  Extended terrain (hand,
                flow_acc, dist_to_channel_m, strahler_order, curvature,
                landcover, soil_hsg) sampled from GEE per pixel before
                aggregating.  Columns already in pixels_static
                (elevation, slope, twi, dist_to_river_m) used directly.
Seasonal      : ndvi_premonsoon (MODIS MOD13Q1), soil_moisture (ERA5-Land
                volumetric_1), precip_premonsoon (CHIRPS) — per field
                centroid, pre-monsoon window April–June of the flood year.

Pixel-support guard
-------------------
All rows are written to field_dataset.  Fields with n_pixels < MIN_PIXELS
are flagged (excluded from modelling export); threshold printed and
configurable via --min-pixels.

Zero-variance report
--------------------
Variance + summary stats printed for EVERY feature before any drop.  A
feature is dropped from the modelling export ONLY if it is actually
constant (variance == 0 across ALL zones).  soil_moisture and
precip_premonsoon are NOT dropped on assumption.

Per-zone flood_pct diagnostics
-------------------------------
mean / std / min / max of flood_pct across fields per zone, to surface
which zones have internal spatial variation worth ranking.

Usage
-----
python agents/collect_field_dataset.py [options]

Flags
-----
--zone-pattern TEXT   POSIX regex on zone_id (default: .*)
--zone-id TEXT        single zone_id (overrides --zone-pattern)
--min-pixels INT      support threshold (default: 5)
--dry-run             compute + print; do not write to field_dataset
--test                first qualifying zone only; still writes
"""

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import date

import ee
import psycopg2
from dotenv import load_dotenv

load_dotenv()

CONN_STRING = os.environ.get("SUPABASE_CONN_STRING")
if not CONN_STRING:
    print("ERROR: SUPABASE_CONN_STRING not set.")
    sys.exit(1)

GEE_PROJECT = "foundation-flood"
GEE_SA_KEY  = os.path.join(os.path.dirname(__file__), "..", "gcp-key.json")

# Default support threshold
MIN_PIXELS_DEFAULT = 5

# GEE batch size for seasonal sampling
GEE_BATCH = 500

# Pre-monsoon window: April 1 → June 30
PREMONSOON_START_MM_DD = (4, 1)
PREMONSOON_END_MM_DD   = (6, 30)

# Fields of the World — global predictions parquet on Source Cooperative S3,
# queried via DuckDB (anonymous). This is the proven Phase 1 loader
# (notebook 03, CELL 11). There is no working FoTW GEE FeatureCollection asset.
FTW_S3   = "s3://ftw/global-data/predictions/vectors/alpha/results/*.parquet"
FTW_YEAR = 2024   # global layer has 2024 & 2025; pick one to avoid duplicate fields

# Feature names must match feature_config.py exactly
TERRAIN_STATIC_DB = ["elevation", "slope", "twi", "dist_to_river_m"]
# GEE-fetched terrain — must match feature_config.TERRAIN_FEATURES exactly
TERRAIN_GEE = [
    "hand", "flow_acc", "dist_to_channel_m", "strahler_order", "curvature",
]
TERRAIN_CATEGORICAL = ["landcover", "soil_hsg"]
SEASONAL_FEATURES   = ["ndvi_premonsoon", "soil_moisture", "precip_premonsoon"]

# ALL_FEATURES keys must exactly match feature_config.TERRAIN_FEATURES + SEASONAL_FEATURES
ALL_FEATURES = (
    ["elevation", "slope", "hand", "twi", "flow_acc",
     "dist_to_channel_m", "dist_to_river_m", "strahler_order",
     "curvature", "landcover", "soil_hsg"]
    + SEASONAL_FEATURES
)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

DDL_FIELD_DATASET = """
CREATE TABLE IF NOT EXISTS field_dataset (
    field_id            text    NOT NULL,
    flood_event_id      uuid    NOT NULL REFERENCES flood_events(event_id),
    zone_id             text    NOT NULL REFERENCES study_zones(zone_id),
    flood_pct           double precision NOT NULL,
    n_pixels            integer NOT NULL,
    features            jsonb   NOT NULL,
    source_metadata_id  uuid    REFERENCES data_sources(source_id),
    PRIMARY KEY (field_id, flood_event_id)
)
"""

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(DDL_FIELD_DATASET)
    conn.commit()


def ensure_source(conn):
    today    = date.today().isoformat()
    pname    = 'collect_field_dataset — Phase 2 Step 2'
    desc     = (
        'Field-level flood dataset. '
        'flood_pct = fraction of pixels_static pixels (spatially joined '
        'to FoTW 2021 field polygon) labelled is_flooded in pixel_flooded. '
        'Terrain aggregated from pixels_static + GEE (MERIT/Hydro, '
        'ESA WorldCover, HydroSHEDS). '
        'Seasonal: MODIS MOD13Q1 NDVI (pre-monsoon Apr–Jun), '
        'ERA5-Land volumetric_1 soil moisture (Apr–Jun), '
        'CHIRPS precipitation (Apr–Jun). '
        'Source: Fields of the World global predictions (Source Cooperative '
        's3://ftw/global-data, year %d) via DuckDB.' % FTW_YEAR
    )
    with conn.cursor() as cur:
        # Try existing row first
        cur.execute(
            "SELECT source_id::text FROM data_sources WHERE product_name = %(p)s",
            {'p': pname},
        )
        row = cur.fetchone()
        if row:
            # Update version tag
            cur.execute(
                "UPDATE data_sources SET version_tag = %(v)s WHERE product_name = %(p)s",
                {'v': f'run:{today}', 'p': pname},
            )
            conn.commit()
            return row[0]
        # Insert fresh
        cur.execute("""
            INSERT INTO data_sources
                (product_name, version_tag, methodology_desc)
            VALUES
                (%(product_name)s, %(version_tag)s, %(methodology_desc)s)
            RETURNING source_id::text
        """, {
            'product_name':     pname,
            'version_tag':      f'run:{today}',
            'methodology_desc': desc,
        })
        sid = cur.fetchone()[0]
    conn.commit()
    return sid


def load_zones(conn, zone_pattern, zone_id_filter):
    """Return list of zone dicts with SUCCESS flood analyses."""
    if zone_id_filter:
        clause = "sz.zone_id = %(filter)s"
        param  = zone_id_filter
    else:
        clause = "sz.zone_id ~ %(filter)s"
        param  = zone_pattern or '.*'

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT
                sz.zone_id,
                ST_AsGeoJSON(sz.geom) AS zone_geom_json
            FROM study_zones sz
            JOIN zone_flood_analysis zfa ON zfa.zone_id = sz.zone_id
            WHERE zfa.status = 'SUCCESS'
              AND {clause}
            ORDER BY sz.zone_id
        """, {'filter': param})
        rows = cur.fetchall()

    return [
        {'zone_id': r[0], 'zone_geom_json': r[1]}
        for r in rows
    ]


def load_events_for_zone(conn, zone_id):
    """Return flood events for this zone (SUCCESS analyses only)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT
                zfa.flood_event_id::text,
                fe.flood_start
            FROM zone_flood_analysis zfa
            JOIN flood_events fe ON fe.event_id = zfa.flood_event_id
            WHERE zfa.zone_id = %(zone_id)s
              AND zfa.status  = 'SUCCESS'
            ORDER BY fe.flood_start
        """, {'zone_id': zone_id})
        return cur.fetchall()  # [(event_id_str, flood_start_date), ...]


def load_pixels_for_zone(conn, zone_id):
    """
    Return per-pixel terrain from pixels_static for one zone.
    Columns available in DB: elevation, slope, twi, spi, curvature, dist_to_river_m, geom.
    Returns list of dicts.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                pixel_id,
                ST_X(geom)       AS lon,
                ST_Y(geom)       AS lat,
                elevation,
                slope,
                twi,
                spi,
                curvature,
                dist_to_river_m
            FROM pixels_static
            WHERE zone_id = %(zone_id)s
              AND geom IS NOT NULL
        """, {'zone_id': zone_id})
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_flood_labels(conn, zone_id, event_id):
    """
    Return dict: pixel_id -> is_flooded for the given zone × event.
    Joins pixel_flooded to pixels_static to filter by zone.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pf.pixel_id, pf.is_flooded
            FROM pixel_flooded pf
            JOIN pixels_static ps ON ps.pixel_id = pf.pixel_id
            WHERE ps.zone_id       = %(zone_id)s
              AND pf.flood_event_id = %(event_id)s::uuid
        """, {'zone_id': zone_id, 'event_id': event_id})
        return {r[0]: r[1] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# GEE helpers — extended terrain per pixel
# ---------------------------------------------------------------------------

def _build_extended_terrain_image():
    """
    Multi-band GEE image: hand, flow_acc, dist_to_channel_m, strahler_order,
    curvature (spi already in DB but included for completeness).

    Sources:
    - MERIT Hydro v1_0_1: hand (hnd), upa (upstream area), str (Strahler order),
      dst (distance to channel in m)
    - ESA WorldCover v200: landcover class
    - SoilGrids: not in GEE public catalog; soil_hsg proxied as NULL (see notes)
    - SRTM Laplacian curvature (matches select_study_pixels.py)
    """
    merit    = ee.Image('MERIT/Hydro/v1_0_1')
    srtm     = ee.Image('USGS/SRTMGL1_003')
    wc       = ee.ImageCollection('ESA/WorldCover/v200').first().select('Map')

    hand              = merit.select('hnd').rename('hand')
    flow_acc_km2      = merit.select('upa')                  # km²
    flow_acc          = flow_acc_km2.multiply(1e6).rename('flow_acc')  # → m²
    dist_to_channel   = merit.select('dst').rename('dist_to_channel_m')
    strahler          = merit.select('str').rename('strahler_order')

    lap_kern  = ee.Kernel.fixed(3, 3, [[0, 1, 0], [1, -4, 1], [0, 1, 0]])
    curvature = srtm.convolve(lap_kern).rename('curvature_gee')

    landcover = wc.rename('landcover')

    return (hand
            .addBands(flow_acc)
            .addBands(dist_to_channel)
            .addBands(strahler)
            .addBands(curvature)
            .addBands(landcover))


def sample_extended_terrain(pixels):
    """
    Sample extended terrain at pixel centroids.
    pixels: list of dicts with pixel_id, lon, lat.
    Returns dict: pixel_id -> {band: value, ...}
    """
    img    = _build_extended_terrain_image()
    bands  = ['hand', 'flow_acc', 'dist_to_channel_m', 'strahler_order',
              'curvature_gee', 'landcover']
    result = {}

    for start in range(0, len(pixels), GEE_BATCH):
        batch    = pixels[start:start + GEE_BATCH]
        features = [
            ee.Feature(ee.Geometry.Point([p['lon'], p['lat']]),
                       {'pixel_id': p['pixel_id']})
            for p in batch
        ]
        fc      = ee.FeatureCollection(features)
        sampled = img.select(bands).sampleRegions(
            collection=fc, properties=['pixel_id'], scale=30, tileScale=4
        )
        for feat in sampled.getInfo().get('features', []):
            props = feat['properties']
            pid   = props['pixel_id']
            result[pid] = {
                'hand':             props.get('hand'),
                'flow_acc':         props.get('flow_acc'),
                'dist_to_channel_m': props.get('dist_to_channel_m'),
                'strahler_order':   props.get('strahler_order'),
                'curvature':        props.get('curvature_gee'),
                'landcover':        props.get('landcover'),
                'soil_hsg':         None,   # not in any free GEE catalog
            }
    return result


# ---------------------------------------------------------------------------
# GEE helpers — FoTW fields
# ---------------------------------------------------------------------------

def load_fotw_fields_for_zone(zone_geom_json):
    """
    Return list of FoTW field dicts that intersect the zone.
    Each dict: {field_id, geom_json, centroid_lon, centroid_lat}

    Source: Fields of the World global predictions parquet on Source Cooperative
    S3, queried via DuckDB (anonymous access), filtered to the zone bounding box
    and FTW_YEAR. Mirrors the proven Phase 1 loader (notebook 03, CELL 11) — the
    GEE FeatureCollection asset does not exist, so S3/DuckDB is the supported path.

    field_id is a stable md5 of the field geometry WKB, so re-runs upsert the same
    rows (idempotent). Centroids computed client-side via shapely.
    """
    import duckdb
    import hashlib
    from shapely import from_wkb
    from shapely.geometry import shape, mapping

    zone_shape = shape(json.loads(zone_geom_json))
    xmin, ymin, xmax, ymax = zone_shape.bounds

    con = duckdb.connect()
    try:
        for stmt in [
            "INSTALL spatial; LOAD spatial;",
            "INSTALL httpfs; LOAD httpfs;",
            "SET s3_endpoint='data.source.coop';",
            "SET s3_url_style='path';",
            "SET s3_region='us-east-1';",
            "SET s3_access_key_id='';",
            "SET s3_secret_access_key='';",
            "SET s3_use_ssl=true;",
        ]:
            con.execute(stmt)

        query = f"""
            SELECT ST_AsWKB(geometry) AS wkb
            FROM read_parquet('{FTW_S3}', hive_partitioning=1)
            WHERE label = 'field'
              AND struct_extract(bbox, 'xmax') >= {xmin}
              AND struct_extract(bbox, 'xmin') <= {xmax}
              AND struct_extract(bbox, 'ymax') >= {ymin}
              AND struct_extract(bbox, 'ymin') <= {ymax}
              AND time >= '{FTW_YEAR}-01-01'
              AND time <  '{FTW_YEAR + 1}-01-01'
        """
        rows = con.execute(query).fetchall()
    finally:
        con.close()

    fields = []
    for (wkb,) in rows:
        if wkb is None:
            continue
        try:
            wkb_bytes = bytes(wkb)
            poly      = from_wkb(wkb_bytes)
            if poly.is_empty or not poly.intersects(zone_shape):
                continue
            centroid  = poly.centroid
            geom_json = json.dumps(mapping(poly))
            fid       = f"ftw{FTW_YEAR}_{hashlib.md5(wkb_bytes).hexdigest()[:16]}"
        except Exception:
            continue
        fields.append({
            'field_id':     fid,
            'geom_json':    geom_json,
            'centroid_lon': centroid.x,
            'centroid_lat': centroid.y,
        })
    return fields


# ---------------------------------------------------------------------------
# GEE helpers — seasonal features
# ---------------------------------------------------------------------------

def _premonsoon_window(flood_year):
    """Return (start_str, end_str) for pre-monsoon window of flood_year."""
    sm, sd = PREMONSOON_START_MM_DD
    em, ed = PREMONSOON_END_MM_DD
    return (f"{flood_year}-{sm:02d}-{sd:02d}",
            f"{flood_year}-{em:02d}-{ed:02d}")


def sample_seasonal_for_fields(fields, flood_year):
    """
    Sample NDVI, soil moisture, precip for a list of field centroids
    for the pre-monsoon window of flood_year.

    Returns dict: field_id -> {ndvi_premonsoon, soil_moisture, precip_premonsoon}

    Sources
    -------
    ndvi_premonsoon    : MODIS/006/MOD13Q1 NDVI band, mean Apr–Jun
    soil_moisture      : ECMWF/ERA5_LAND/MONTHLY_AGGR
                         volumetric_soil_water_layer_1, mean Apr–Jun
    precip_premonsoon  : UCSB-CHG/CHIRPS/DAILY total Apr–Jun (mm)
    """
    start_str, end_str = _premonsoon_window(flood_year)

    # NDVI
    ndvi_img = (
        ee.ImageCollection('MODIS/006/MOD13Q1')
        .filterDate(start_str, end_str)
        .select('NDVI')
        .mean()
        .multiply(0.0001)   # scale factor
        .rename('ndvi_premonsoon')
    )

    # Soil moisture (ERA5-Land monthly — take months 4,5,6)
    sm_img = (
        ee.ImageCollection('ECMWF/ERA5_LAND/MONTHLY_AGGR')
        .filterDate(start_str, end_str)
        .select('volumetric_soil_water_layer_1')
        .mean()
        .rename('soil_moisture')
    )

    # Precipitation total (CHIRPS daily sum)
    precip_img = (
        ee.ImageCollection('UCSB-CHG/CHIRPS/DAILY')
        .filterDate(start_str, end_str)
        .select('precipitation')
        .sum()
        .rename('precip_premonsoon')
    )

    combined = ndvi_img.addBands(sm_img).addBands(precip_img)
    bands    = ['ndvi_premonsoon', 'soil_moisture', 'precip_premonsoon']

    result = {}
    for start in range(0, len(fields), GEE_BATCH):
        batch = fields[start:start + GEE_BATCH]
        features = [
            ee.Feature(
                ee.Geometry.Point([f['centroid_lon'], f['centroid_lat']]),
                {'field_id': f['field_id']}
            )
            for f in batch
            if f['centroid_lon'] is not None
        ]
        if not features:
            continue
        fc      = ee.FeatureCollection(features)
        sampled = combined.sampleRegions(
            collection=fc, properties=['field_id'],
            scale=100, tileScale=4
        )
        for feat in sampled.getInfo().get('features', []):
            props = feat['properties']
            fid   = props['field_id']
            result[fid] = {
                'ndvi_premonsoon':   props.get('ndvi_premonsoon'),
                'soil_moisture':     props.get('soil_moisture'),
                'precip_premonsoon': props.get('precip_premonsoon'),
            }
    return result


# ---------------------------------------------------------------------------
# Spatial join: assign each pixel to a FoTW field
# ---------------------------------------------------------------------------

def assign_pixels_to_fields(pixels, fields):
    """
    Pure Python spatial join using shapely.
    Returns dict: pixel_id -> field_id (or None if no field covers the pixel).
    """
    from shapely.geometry import Point, shape

    field_shapes = []
    for f in fields:
        try:
            poly = shape(json.loads(f['geom_json']))
            field_shapes.append((f['field_id'], poly))
        except Exception:
            continue

    pixel_to_field = {}
    for p in pixels:
        pt    = Point(p['lon'], p['lat'])
        found = None
        for fid, poly in field_shapes:
            if poly.contains(pt):
                found = fid
                break
        pixel_to_field[p['pixel_id']] = found

    return pixel_to_field


# ---------------------------------------------------------------------------
# Aggregation: pixels → field
# ---------------------------------------------------------------------------

def _mode(values):
    """Return modal value from a list; None if empty."""
    if not values:
        return None
    counter = Counter(v for v in values if v is not None)
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _mean(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _std(values):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    m   = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    return math.sqrt(var)


def aggregate_field_terrain(pixels_in_field, ext_terrain):
    """
    Aggregate pixel-level terrain to a single field row.

    Continuous: mean
    Categorical (landcover, soil_hsg): mode

    Returns dict of feature_name -> value.
    """
    def collect(key):
        return [p.get(key) for p in pixels_in_field]

    def collect_ext(key):
        return [ext_terrain.get(p['pixel_id'], {}).get(key)
                for p in pixels_in_field]

    feats = {
        # DB-native continuous
        'elevation':       _mean(collect('elevation')),
        'slope':           _mean(collect('slope')),
        'twi':             _mean(collect('twi')),
        'dist_to_river_m': _mean(collect('dist_to_river_m')),
        # GEE-extended continuous
        'hand':             _mean(collect_ext('hand')),
        'flow_acc':         _mean(collect_ext('flow_acc')),
        'dist_to_channel_m': _mean(collect_ext('dist_to_channel_m')),
        'strahler_order':   _mean(collect_ext('strahler_order')),
        'curvature':        _mean(collect_ext('curvature')),
        'spi':              _mean(collect('spi')),
        # GEE-extended categorical (mode)
        'landcover':  _mode(collect_ext('landcover')),
        'soil_hsg':   _mode(collect_ext('soil_hsg')),
    }
    return feats


# ---------------------------------------------------------------------------
# Variance report
# ---------------------------------------------------------------------------

def print_variance_report(all_rows):
    """
    Print variance + summary stats for every feature across all rows.
    Returns set of feature names that are actually constant (variance == 0).
    """
    if not all_rows:
        print("\nVARIANCE REPORT: no rows to analyse.")
        return set()

    print("\n" + "=" * 70)
    print("VARIANCE REPORT")
    print("=" * 70)

    feature_vals = defaultdict(list)
    for row in all_rows:
        feats = row.get('features', {})
        for fname in ALL_FEATURES:
            v = feats.get(fname)
            if v is not None:
                feature_vals[fname].append(v)

    constant_features = set()

    header = f"{'Feature':<25} {'N':>6} {'Mean':>12} {'Std':>12} {'Min':>12} {'Max':>12} {'Var=0?':>8}"
    print(header)
    print("-" * 90)

    for fname in ALL_FEATURES:
        vals = feature_vals[fname]
        n    = len(vals)
        if n == 0:
            print(f"  {fname:<23} {'0':>6} {'—':>12} {'—':>12} {'—':>12} {'—':>12} {'—':>8}")
            continue

        # For categorical features: report count of unique values instead
        if fname in TERRAIN_CATEGORICAL:
            unique_vals = set(vals)
            n_unique    = len(unique_vals)
            is_const    = (n_unique == 1)
            if is_const:
                constant_features.add(fname)
            print(f"  {fname:<23} {n:>6} {'(categorical)':>12} "
                  f"  n_unique={n_unique:>4}  const={is_const}")
            continue

        # Numeric
        try:
            numeric = [float(v) for v in vals]
        except (TypeError, ValueError):
            print(f"  {fname:<23} {n:>6} {'(non-numeric)':>12}")
            continue

        mean_v = sum(numeric) / len(numeric)
        var_v  = sum((v - mean_v) ** 2 for v in numeric) / len(numeric)
        std_v  = math.sqrt(var_v)
        min_v  = min(numeric)
        max_v  = max(numeric)
        is_const = (var_v == 0.0)
        if is_const:
            constant_features.add(fname)
        const_flag = "YES" if is_const else ""

        print(f"  {fname:<23} {n:>6} {mean_v:>12.4f} {std_v:>12.4f} "
              f"{min_v:>12.4f} {max_v:>12.4f} {const_flag:>8}")

    print("=" * 70)
    if constant_features:
        print(f"  Constant features (dropped from modelling export): "
              f"{sorted(constant_features)}")
    else:
        print("  No constant features — all retained in modelling export.")
    print("=" * 70 + "\n")

    return constant_features


def print_support_distribution(all_rows, min_pixels):
    """Print distribution of n_pixels per field; flag below-support count."""
    if not all_rows:
        print("\nSUPPORT DISTRIBUTION: no rows.")
        return 0

    counts = [r['n_pixels'] for r in all_rows]
    below  = sum(1 for c in counts if c < min_pixels)
    total  = len(counts)

    # Histogram: 0, 1-2, 3-4, 5-9, 10-19, 20-49, 50+
    buckets = [
        (0,  0,   "0"),
        (1,  2,   "1-2"),
        (3,  4,   "3-4"),
        (5,  9,   "5-9"),
        (10, 19,  "10-19"),
        (20, 49,  "20-49"),
        (50, 9999, "50+"),
    ]

    print("\n" + "=" * 50)
    print("PIXEL-SUPPORT DISTRIBUTION")
    print(f"  MIN_PIXELS threshold = {min_pixels}")
    print(f"  Total rows           = {total}")
    print(f"  Below threshold      = {below}  ({100*below/total:.1f}%)")
    print("-" * 50)
    print(f"  {'n_pixels range':<18} {'count':>7} {'%':>8}")
    for lo, hi, label in buckets:
        n = sum(1 for c in counts if lo <= c <= hi)
        print(f"  {label:<18} {n:>7} {100*n/total:>7.1f}%")
    print("=" * 50 + "\n")
    return below


def print_zone_flood_pct_spread(zone_rows):
    """
    For each zone, print flood_pct distribution (mean/std/min/max)
    across all fields in that zone (all events combined).
    """
    print("\n" + "=" * 70)
    print("PER-ZONE FLOOD_PCT SPREAD (across fields × events)")
    print(f"  {'zone_id':<30} {'n_rows':>7} {'mean':>8} {'std':>8} {'min':>8} {'max':>8}")
    print("-" * 70)

    for zone_id in sorted(zone_rows.keys()):
        rows   = zone_rows[zone_id]
        values = [r['flood_pct'] for r in rows]
        n      = len(values)
        if n == 0:
            continue
        mean_v = sum(values) / n
        var_v  = sum((v - mean_v) ** 2 for v in values) / n
        std_v  = math.sqrt(var_v)
        min_v  = min(values)
        max_v  = max(values)
        print(f"  {zone_id:<30} {n:>7} {mean_v:>8.3f} {std_v:>8.3f} "
              f"{min_v:>8.3f} {max_v:>8.3f}")

    print("=" * 70 + "\n")


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

def write_rows(conn, rows, source_id, dry_run):
    """Upsert rows into field_dataset. rows: list of dicts."""
    if dry_run:
        return

    with conn.cursor() as cur:
        for row in rows:
            cur.execute("""
                INSERT INTO field_dataset
                    (field_id, flood_event_id, zone_id, flood_pct,
                     n_pixels, features, source_metadata_id)
                VALUES
                    (%(field_id)s, %(flood_event_id)s::uuid, %(zone_id)s,
                     %(flood_pct)s, %(n_pixels)s, %(features)s::jsonb,
                     %(source_id)s::uuid)
                ON CONFLICT (field_id, flood_event_id) DO UPDATE
                    SET zone_id            = EXCLUDED.zone_id,
                        flood_pct          = EXCLUDED.flood_pct,
                        n_pixels           = EXCLUDED.n_pixels,
                        features           = EXCLUDED.features,
                        source_metadata_id = EXCLUDED.source_metadata_id
            """, {
                'field_id':       row['field_id'],
                'flood_event_id': row['flood_event_id'],
                'zone_id':        row['zone_id'],
                'flood_pct':      row['flood_pct'],
                'n_pixels':       row['n_pixels'],
                'features':       json.dumps(row['features']),
                'source_id':      source_id,
            })
    conn.commit()


# ---------------------------------------------------------------------------
# Per-zone processing
# ---------------------------------------------------------------------------

def process_zone(zone, conn, source_id, min_pixels, dry_run):
    """
    Process one zone: load fields, pixels, labels, aggregate, return rows.
    Returns list of row dicts.
    """
    zone_id       = zone['zone_id']
    zone_geom_json = zone['zone_geom_json']

    # --- Load flood events ---
    events = load_events_for_zone(conn, zone_id)
    if not events:
        print(f"  No SUCCESS flood events — skipping.")
        return []

    print(f"  Flood events  : {len(events)}")

    # --- Load FoTW fields for this zone ---
    print(f"  Loading FoTW fields from GEE ...", end='', flush=True)
    fields = load_fotw_fields_for_zone(zone_geom_json)
    if not fields:
        print(" 0 fields — skipping zone.")
        return []
    print(f" {len(fields)} fields")

    # --- Load pixels from DB ---
    pixels = load_pixels_for_zone(conn, zone_id)
    print(f"  Pixels in DB  : {len(pixels)}")
    if not pixels:
        print(f"  No pixels for zone — skipping.")
        return []

    # --- Sample extended terrain from GEE ---
    print(f"  Sampling extended terrain (GEE) for {len(pixels)} pixels ...",
          end='', flush=True)
    ext_terrain = sample_extended_terrain(pixels)
    print(f" {len(ext_terrain)} results")

    # --- Spatial join: pixel → field ---
    print(f"  Spatial join pixels → fields ...", end='', flush=True)
    pixel_to_field = assign_pixels_to_fields(pixels, fields)
    covered_pixels = sum(1 for v in pixel_to_field.values() if v is not None)
    print(f" {covered_pixels}/{len(pixels)} pixels matched to a field")

    # Group pixels by field
    field_pixels = defaultdict(list)
    for p in pixels:
        fid = pixel_to_field.get(p['pixel_id'])
        if fid:
            field_pixels[fid].append(p)

    # --- Determine unique flood years for seasonal sampling ---
    flood_years = sorted({e[1].year for e in events})
    year_seasonal = {}
    for yr in flood_years:
        print(f"  Sampling seasonal features for year {yr} ...", end='', flush=True)
        year_seasonal[yr] = sample_seasonal_for_fields(fields, yr)
        print(f" {len(year_seasonal[yr])} field results")

    # --- Aggregate per field × event ---
    rows = []
    for event_id, flood_start in events:
        flood_year = flood_start.year
        seasonal   = year_seasonal.get(flood_year, {})

        # Load pixel flood labels for this event
        labels = load_flood_labels(conn, zone_id, event_id)
        if not labels:
            print(f"  Event {event_id[:8]}... — no pixel labels, skipping.")
            continue

        for fid, fpixels in field_pixels.items():
            n_pixels = len(fpixels)

            # Count flooded pixels
            flooded   = sum(1 for p in fpixels
                            if labels.get(p['pixel_id']) is True)
            labelled  = sum(1 for p in fpixels
                            if labels.get(p['pixel_id']) is not None)
            if labelled == 0:
                continue
            flood_pct = flooded / labelled

            # Terrain features
            terrain_feats = aggregate_field_terrain(fpixels, ext_terrain)

            # Seasonal features (per field centroid)
            s = seasonal.get(fid, {})
            terrain_feats['ndvi_premonsoon']   = s.get('ndvi_premonsoon')
            terrain_feats['soil_moisture']     = s.get('soil_moisture')
            terrain_feats['precip_premonsoon'] = s.get('precip_premonsoon')

            rows.append({
                'field_id':       fid,
                'flood_event_id': event_id,
                'zone_id':        zone_id,
                'flood_pct':      flood_pct,
                'n_pixels':       n_pixels,
                'features':       terrain_feats,
            })

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Collect field-level flood dataset (Phase 2 Step 2)."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '--zone-id',
        help='Single zone_id to process',
    )
    group.add_argument(
        '--zone-pattern', default='.*',
        help="POSIX regex on zone_id (default: '.*')",
    )
    parser.add_argument(
        '--min-pixels', type=int, default=MIN_PIXELS_DEFAULT,
        help=f'Pixel-support threshold (default: {MIN_PIXELS_DEFAULT})',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Compute and print; do not write to field_dataset',
    )
    parser.add_argument(
        '--test', action='store_true',
        help='Process first qualifying zone only; still writes',
    )
    args = parser.parse_args()

    print(f"Zone filter   : {args.zone_id or args.zone_pattern}")
    print(f"Min pixels    : {args.min_pixels}")
    print(f"Dry-run       : {args.dry_run}")
    print(f"Test          : {args.test}")
    print()

    # --- GEE auth ---
    key_path = GEE_SA_KEY
    if os.path.exists(key_path):
        with open(key_path) as f:
            key_data = json.load(f)
        creds = ee.ServiceAccountCredentials(key_data['client_email'], key_path)
        ee.Initialize(creds, project=GEE_PROJECT)
        print("GEE: authenticated via service account.")
    else:
        ee.Initialize(project=GEE_PROJECT)
        print("GEE: authenticated via user credentials.")

    # --- DB setup ---
    try:
        with psycopg2.connect(CONN_STRING) as conn:
            if not args.dry_run:
                ensure_table(conn)
            source_id = ensure_source(conn) if not args.dry_run else None

            zones = load_zones(conn, args.zone_pattern, args.zone_id)

    except psycopg2.Error as exc:
        print(f"DB error: {exc}")
        sys.exit(1)

    if not zones:
        print("No zones with SUCCESS flood analyses found.")
        sys.exit(0)

    print(f"Zones to process: {len(zones)}")
    if args.test and len(zones) > 1:
        print(f"TEST mode: limiting to first zone of {len(zones)}.\n")
        zones = zones[:1]

    # --- Per-zone loop ---
    all_rows            = []
    zone_rows_map       = {}
    total_written       = 0
    total_below_support = 0
    zone_errors         = 0

    for idx, zone in enumerate(zones):
        zone_id = zone['zone_id']
        print(f"\n[{idx+1}/{len(zones)}] Zone: {zone_id}")

        try:
            with psycopg2.connect(CONN_STRING) as conn:
                rows = process_zone(zone, conn, source_id, args.min_pixels, args.dry_run)

            if not rows:
                print(f"  Zone produced 0 rows.")
                continue

            zone_rows_map[zone_id] = rows
            all_rows.extend(rows)

            below = sum(1 for r in rows if r['n_pixels'] < args.min_pixels)
            print(f"  Rows: {len(rows)}  below-support (n_pixels<{args.min_pixels}): {below}")

            # Write all rows (below-support included; model harness filters by n_pixels)
            if not args.dry_run:
                with psycopg2.connect(CONN_STRING) as conn:
                    write_rows(conn, rows, source_id, dry_run=False)
                print(f"  Written: {len(rows)} rows → field_dataset")

            total_written       += len(rows)
            total_below_support += below

        except Exception as exc:
            import traceback
            print(f"  ERROR in zone {zone_id}: {exc}")
            traceback.print_exc()
            zone_errors += 1
            continue

    # --- Diagnostics ---
    print_zone_flood_pct_spread(zone_rows_map)
    print_support_distribution(all_rows, args.min_pixels)
    constant_features = print_variance_report(all_rows)

    # --- Final summary ---
    print("=" * 70)
    print("SUMMARY")
    print(f"  Zones processed       : {len(zones) - zone_errors}/{len(zones)}")
    print(f"  Zones with errors     : {zone_errors}")
    print(f"  Total rows            : {total_written}")
    print(f"  Below-support rows    : {total_below_support}  "
          f"(n_pixels < {args.min_pixels}, stored but flagged)")
    print(f"  Modelling rows        : {total_written - total_below_support}")
    if constant_features:
        print(f"  Dropped features      : {sorted(constant_features)}  "
              f"(actually constant — zero variance)")
    else:
        print(f"  Dropped features      : none")
    if args.dry_run:
        print("  DRY-RUN: no data written to field_dataset")
    print("=" * 70)

    sys.exit(0 if zone_errors == 0 else 1)


if __name__ == '__main__':
    main()
