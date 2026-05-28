#!/usr/bin/env python3
"""
select_study_pixels.py — Sample Sentinel-grid-aligned agricultural pixels per study zone.

Places subzone squares at increasing distances from the river in distance bands of
subzone_width_m, starting at min_dist_m. Picks `reps` random positions per band
until `subzones` total squares are placed. Agricultural pixels (ESA WorldCover
class 40) within each subzone are written to pixels_static, then terrain metrics
(elevation, slope, twi, spi, curvature, dist_to_river_m) are populated via GEE
and PostGIS.

Zone selection:
  --zone-id    process a single zone by zone_id
  --zone-pattern  POSIX regex matched against zone_id for zones that have at least
                  one SUCCESS row in zone_flood_analysis

Creates study_zone_dataset (if absent) and adds geom + study_zone_dataset_id
columns to pixels_static (if absent) before inserting.

Usage:
    python agents/select_study_pixels.py --zone-id ZONE_ID [options]
    python agents/select_study_pixels.py --zone-pattern REGEX [options]

Options:
    --zone-id TEXT          Single study_zones.zone_id to process
    --zone-pattern TEXT     POSIX regex; process all zones with SUCCESS floods
                            whose zone_id matches (e.g. 'initial')
    --subzones INT          Total subzone squares to place (default: 12)
    --reps INT              Random placements per distance band (default: 2)
    --subzone-width INT     Side length of each subzone square in metres (default: 500)
    --min-dist INT          Minimum distance from river in metres (default: 100)
    --max-pixels INT        Max pixels to keep per subzone, randomly sampled (0 = no limit, default: 500)
    --seed INT              Random seed for reproducibility (optional)
    --test                  Process only the first qualifying subzone per zone
"""

import argparse
import json
import math
import os
import random
import sys

import ee
import psycopg2
from dotenv import load_dotenv
from pyproj import Transformer
from shapely.geometry import Point, box, shape
from shapely.ops import transform as shapely_transform

load_dotenv()

CONN_STRING = os.environ.get("SUPABASE_CONN_STRING")
if not CONN_STRING:
    print("ERROR: SUPABASE_CONN_STRING not set.")
    sys.exit(1)

UTM42N = 32642          # EPSG for Pakistan (UTM zone 42N)
TERRAIN_BATCH = 500     # max pixels per GEE sampleRegions call

DDL_STUDY_ZONE_DATASET = """
CREATE TABLE IF NOT EXISTS study_zone_dataset (
    dataset_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    zone_id          text NOT NULL REFERENCES study_zones(zone_id),
    subzones         integer NOT NULL,
    reps             integer NOT NULL,
    subzone_width_m  integer NOT NULL,
    min_dist_m       integer NOT NULL,
    agri_metadata_id uuid REFERENCES data_sources(source_id),
    topo_metadata_id uuid REFERENCES data_sources(source_id),
    created_at       timestamptz DEFAULT now()
)
"""

AGRI_SOURCE = {
    'product_name':    'ESA WorldCover v200 — Cropland',
    'version_tag':     'v200 (2021)',
    'resolution_m':    10.0,
    'methodology_desc': (
        'GEE asset ESA/WorldCover/v200 loaded as ImageCollection; .first() selects the '
        'single 2021 mosaic tile. Band "Map" contains LULC class integers. '
        'Cropland pixels isolated with .eq(40).selfMask() (class 40 = Cropland). '
        'Sampled with .sample(region=subzone_bbox, scale=10, geometries=True, tileScale=4), '
        'max 10 000 results per 500 m x 500 m subzone. '
        'Returned (lon, lat) centroids are 10 m Sentinel-2-grid-aligned pixel centres.'
    ),
}

TOPO_SOURCE = {
    'product_name':    'SRTM + MERIT Hydro — Terrain Metrics',
    'version_tag':     'SRTM 1-arc-second; MERIT Hydro v1.0.1',
    'resolution_m':    30.0,
    'methodology_desc': (
        'elevation: USGS/SRTMGL1_003 band "elevation" via ee.Terrain.products(), metres. '
        'slope: ee.Terrain.products() "slope" band, degrees. '
        'TWI: ln(upa_m2 / tan(slope_rad)); '
        'upa_m2 = MERIT/Hydro/v1_0_1 band "upa" (upstream area km^2) x 1e6; '
        'slope_rad = slope_deg x pi/180; tan(slope) floored at 1e-6. '
        'SPI: upa_m2 x tan(slope_rad). '
        'curvature: SRTM convolved with 3x3 Laplacian kernel [[0,1,0],[1,-4,1],[0,1,0]] '
        'via ee.Kernel.fixed(); units = elevation change per pixel^2. '
        'dist_to_river_m: PostGIS ST_Distance(pixel.geom::geography, river.geom::geography) '
        'joining pixels_static -> study_zone_dataset -> study_zones -> rivers. '
        'All GEE bands sampled at scale=30 m via sampleRegions().'
    ),
}


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(DDL_STUDY_ZONE_DATASET)
        # Re-point study_zone_dataset FKs from old data_source → data_sources
        cur.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'study_zone_dataset_agri_metadata_id_fkey'
                      AND confrelid = 'data_source'::regclass
                ) THEN
                    ALTER TABLE study_zone_dataset
                        DROP CONSTRAINT study_zone_dataset_agri_metadata_id_fkey;
                END IF;
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'study_zone_dataset_topo_metadata_id_fkey'
                      AND confrelid = 'data_source'::regclass
                ) THEN
                    ALTER TABLE study_zone_dataset
                        DROP CONSTRAINT study_zone_dataset_topo_metadata_id_fkey;
                END IF;
            END $$
        """)
        cur.execute("""
            ALTER TABLE study_zone_dataset
                ADD COLUMN IF NOT EXISTS agri_metadata_id uuid REFERENCES data_sources(source_id),
                ADD COLUMN IF NOT EXISTS topo_metadata_id  uuid REFERENCES data_sources(source_id)
        """)
        cur.execute("""
            ALTER TABLE pixels_static
                ADD COLUMN IF NOT EXISTS study_zone_dataset_id uuid
                    REFERENCES study_zone_dataset(dataset_id),
                ADD COLUMN IF NOT EXISTS geom             geometry(Point, 4326),
                ADD COLUMN IF NOT EXISTS elevation        double precision,
                ADD COLUMN IF NOT EXISTS slope            double precision,
                ADD COLUMN IF NOT EXISTS twi              double precision,
                ADD COLUMN IF NOT EXISTS spi              double precision,
                ADD COLUMN IF NOT EXISTS curvature        double precision,
                ADD COLUMN IF NOT EXISTS dist_to_river_m  double precision
        """)
    conn.commit()


def ensure_data_sources(conn):
    """Upsert known data sources into data_sources; return (agri_source_id, topo_source_id)."""
    ids = {}
    with conn.cursor() as cur:
        for key, src in [('agri', AGRI_SOURCE), ('topo', TOPO_SOURCE)]:
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
            ids[key] = cur.fetchone()[0]
    conn.commit()
    return ids['agri'], ids['topo']


def load_zone(conn, zone_id):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                sz.zone_id,
                sz.station_id::text,
                sz.zone_size_m,
                ST_AsGeoJSON(sz.geom)  AS zone_geom_json,
                ST_AsGeoJSON(r.geom)   AS river_geom_json
            FROM study_zones sz
            JOIN rivers r ON r.river_id = sz.river_id
            WHERE sz.zone_id = %(zone_id)s
        """, {'zone_id': zone_id})
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"zone_id '{zone_id}' not found in study_zones "
                         "(or has no linked river).")
    return {
        'zone_id':     row[0],
        'station_id':  row[1],
        'zone_size_m': row[2],
        'zone_geom':   shape(json.loads(row[3])),
        'river_geom':  shape(json.loads(row[4])),
    }


def load_zones_for_pattern(conn, pattern):
    """Return zone dicts for all zones whose zone_id matches the POSIX regex
    and that have at least one SUCCESS row in zone_flood_analysis."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT
                sz.zone_id,
                sz.station_id::text,
                sz.zone_size_m,
                ST_AsGeoJSON(sz.geom) AS zone_geom_json,
                ST_AsGeoJSON(r.geom)  AS river_geom_json
            FROM zone_flood_analysis zfa
            JOIN study_zones sz ON sz.zone_id = zfa.zone_id
            JOIN rivers r ON r.river_id = sz.river_id
            WHERE zfa.status = 'SUCCESS'
              AND sz.zone_id ~ %(pattern)s
            ORDER BY sz.zone_id
        """, {'pattern': pattern})
        rows = cur.fetchall()
    return [
        {
            'zone_id':     row[0],
            'station_id':  row[1],
            'zone_size_m': row[2],
            'zone_geom':   shape(json.loads(row[3])),
            'river_geom':  shape(json.loads(row[4])),
        }
        for row in rows
    ]


def create_dataset(conn, zone_id, subzones, reps, subzone_width, min_dist,
                   agri_metadata_id, topo_metadata_id):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO study_zone_dataset
                (zone_id, subzones, reps, subzone_width_m, min_dist_m,
                 agri_metadata_id, topo_metadata_id)
            VALUES
                (%(zone_id)s, %(subzones)s, %(reps)s, %(subzone_width_m)s, %(min_dist_m)s,
                 %(agri_metadata_id)s::uuid, %(topo_metadata_id)s::uuid)
            RETURNING dataset_id::text
        """, {'zone_id': zone_id, 'subzones': subzones, 'reps': reps,
              'subzone_width_m': subzone_width, 'min_dist_m': min_dist,
              'agri_metadata_id': agri_metadata_id, 'topo_metadata_id': topo_metadata_id})
        return cur.fetchone()[0]


def build_subzone_centres(zone_geom, river_geom, subzones, reps, subzone_width, min_dist):
    """
    Generate subzone centre points in WGS84.

    Distance bands start at min_dist from the river, each band is subzone_width wide.
    Within each band, pick `reps` random positions by rejection-sampling within the
    band-annulus intersected with the study zone.
    """
    to_utm = Transformer.from_crs(4326, UTM42N, always_xy=True).transform
    to_wgs = Transformer.from_crs(UTM42N, 4326, always_xy=True).transform

    zone_utm  = shapely_transform(to_utm, zone_geom)
    river_utm = shapely_transform(to_utm, river_geom)

    num_bands = math.ceil(subzones / reps)
    half_sw   = subzone_width / 2
    centres   = []

    for band_i in range(num_bands):
        if len(centres) >= subzones:
            break

        inner = min_dist + band_i * subzone_width
        outer = min_dist + (band_i + 1) * subzone_width

        ring_inner = river_utm.buffer(inner)
        ring_outer = river_utm.buffer(outer)
        annulus    = ring_outer.difference(ring_inner)

        # Shrink zone inward so the placed square fits within the study zone boundary.
        valid = zone_utm.buffer(-half_sw).intersection(annulus)

        if valid.is_empty:
            continue

        bounds       = valid.bounds
        band_found   = 0
        attempts     = 0
        max_attempts = reps * 500

        while band_found < reps and len(centres) < subzones and attempts < max_attempts:
            x  = random.uniform(bounds[0], bounds[2])
            y  = random.uniform(bounds[1], bounds[3])
            pt = Point(x, y)
            if valid.contains(pt):
                centres.append(pt)
                band_found += 1
            attempts += 1

        if band_found < reps:
            print(f"  Band {band_i} (inner={inner}m outer={outer}m): "
                  f"found {band_found}/{reps} positions after {attempts} attempts")

    return [shapely_transform(to_wgs, pt) for pt in centres]


def subzone_box_wgs(centre_wgs, subzone_width):
    to_utm = Transformer.from_crs(4326, UTM42N, always_xy=True).transform
    to_wgs = Transformer.from_crs(UTM42N, 4326, always_xy=True).transform
    cx, cy  = to_utm(centre_wgs.x, centre_wgs.y)
    half    = subzone_width / 2
    sq_utm  = box(cx - half, cy - half, cx + half, cy + half)
    return shapely_transform(to_wgs, sq_utm)


def sample_agri_pixels(subzone_wgs):
    """Return list of (lon, lat) for ESA WorldCover cropland (class 40) pixels."""
    coords     = list(subzone_wgs.exterior.coords)
    ee_geom    = ee.Geometry.Polygon(coords)
    worldcover = ee.ImageCollection('ESA/WorldCover/v200').first().select('Map')
    samples    = (worldcover.eq(40).selfMask()
                  .sample(region=ee_geom, scale=10, geometries=True, tileScale=4))
    fc_info    = samples.limit(10_000).getInfo()
    return [feat['geometry']['coordinates'] for feat in fc_info.get('features', [])]


def pixel_id_from_lonlat(dataset_id_short, lon, lat):
    to_utm = Transformer.from_crs(4326, UTM42N, always_xy=True).transform
    e, n   = to_utm(lon, lat)
    e10    = int(round(e / 10))
    n10    = int(round(n / 10))
    return f"{dataset_id_short}_{e10}_{n10}"


def insert_pixels(conn, pixels, dataset_id, zone_id, agri_id):
    dataset_id_short = dataset_id.replace('-', '')[:12]
    with conn.cursor() as cur:
        for lon, lat in pixels:
            pixel_id = pixel_id_from_lonlat(dataset_id_short, lon, lat)
            cur.execute("""
                INSERT INTO pixels_static
                    (pixel_id, zone_id, study_zone_dataset_id, is_agri, geom, agri_metadata_id)
                VALUES (
                    %(pixel_id)s,
                    %(zone_id)s,
                    %(dataset_id)s::uuid,
                    true,
                    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                    %(agri_id)s::uuid
                )
                ON CONFLICT (pixel_id) DO NOTHING
            """, {'pixel_id': pixel_id, 'zone_id': zone_id,
                  'dataset_id': dataset_id, 'lon': lon, 'lat': lat,
                  'agri_id': agri_id})
    conn.commit()


def _build_terrain_image():
    """
    Build a multi-band GEE image: elevation, slope, twi, spi, curvature.

    - elevation / slope  : SRTM 30m via ee.Terrain.products
    - twi / spi          : MERIT Hydro upstream drainage area + tan(slope)
    - curvature          : Laplacian of SRTM elevation (4-neighbour kernel)
    """
    srtm      = ee.Image('USGS/SRTMGL1_003')
    merit     = ee.Image('MERIT/Hydro/v1_0_1')
    terr      = ee.Terrain.products(srtm)
    elev      = terr.select('elevation')
    slope_deg = terr.select('slope')
    slope_rad = slope_deg.multiply(math.pi / 180)
    upa_m2    = merit.select('upa').multiply(1e6)     # km² → m²
    tan_beta  = slope_rad.tan().max(ee.Image(1e-6))   # guard against zero slope
    twi       = upa_m2.divide(tan_beta).log().rename('twi')
    spi       = upa_m2.multiply(tan_beta).rename('spi')
    lap_kern  = ee.Kernel.fixed(3, 3, [[0, 1, 0], [1, -4, 1], [0, 1, 0]])
    curvature = srtm.convolve(lap_kern).rename('curvature')
    return (elev.rename('elevation')
                .addBands(slope_deg.rename('slope'))
                .addBands(twi)
                .addBands(spi)
                .addBands(curvature))


def populate_terrain_metrics(conn, dataset_id, topo_id):
    """
    Populate elevation, slope, twi, spi, curvature, dist_to_river_m for all
    pixels belonging to dataset_id.

    Terrain bands are batch-sampled from GEE (SRTM + MERIT Hydro).
    dist_to_river_m is computed via PostGIS by walking:
        study_zone_dataset → study_zones → rivers
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pixel_id, ST_X(geom) AS lon, ST_Y(geom) AS lat
            FROM pixels_static
            WHERE study_zone_dataset_id = %(dataset_id)s::uuid
              AND geom IS NOT NULL
        """, {'dataset_id': dataset_id})
        pixels = cur.fetchall()

    if not pixels:
        print("  No pixels to populate terrain for.")
        return

    print(f"  Sampling terrain for {len(pixels)} pixels ... ", end='', flush=True)
    terrain = _build_terrain_image()
    metrics = {}

    for start in range(0, len(pixels), TERRAIN_BATCH):
        batch    = pixels[start:start + TERRAIN_BATCH]
        features = [
            ee.Feature(ee.Geometry.Point([lon, lat]), {'pixel_id': pid})
            for pid, lon, lat in batch
        ]
        fc      = ee.FeatureCollection(features)
        sampled = terrain.sampleRegions(
            collection=fc, properties=['pixel_id'], scale=30
        )
        for feat in sampled.getInfo().get('features', []):
            props = feat['properties']
            pid   = props['pixel_id']
            metrics[pid] = {
                'elevation': props.get('elevation'),
                'slope':     props.get('slope'),
                'twi':       props.get('twi'),
                'spi':       props.get('spi'),
                'curvature': props.get('curvature'),
            }

    print(f"got {len(metrics)} results")

    if not metrics:
        print("  WARNING: GEE returned 0 terrain samples — "
              "check GEE auth, asset availability, and pixel geometries.")
        return

    with conn.cursor() as cur:
        for pid, _lon, _lat in pixels:
            m = metrics.get(pid, {})
            cur.execute("""
                UPDATE pixels_static
                SET elevation        = %(elevation)s,
                    slope            = %(slope)s,
                    twi              = %(twi)s,
                    spi              = %(spi)s,
                    curvature        = %(curvature)s,
                    topo_metadata_id = %(topo_id)s::uuid
                WHERE pixel_id = %(pixel_id)s
            """, {
                'elevation': m.get('elevation'),
                'slope':     m.get('slope'),
                'twi':       m.get('twi'),
                'spi':       m.get('spi'),
                'curvature': m.get('curvature'),
                'topo_id':   topo_id,
                'pixel_id':  pid,
            })

        # dist_to_river_m: follow dataset → study_zones → rivers in one UPDATE
        cur.execute("""
            UPDATE pixels_static ps
            SET dist_to_river_m = ST_Distance(
                ps.geom::geography,
                r.geom::geography
            )
            FROM study_zone_dataset szd
            JOIN study_zones sz ON sz.zone_id  = szd.zone_id
            JOIN rivers r        ON r.river_id = sz.river_id
            WHERE szd.dataset_id                = %(dataset_id)s::uuid
              AND ps.study_zone_dataset_id      = %(dataset_id)s::uuid
        """, {'dataset_id': dataset_id})

    conn.commit()
    print(f"  Terrain metrics written for {len(pixels)} pixels.")


def process_zone(zone, args):
    """Run the full pixel-selection pipeline for one zone. Returns dataset_id."""
    zone_w = zone['zone_size_m']
    print(f"  Study zone    : {zone_w} m × {zone_w} m")

    num_bands = math.ceil(args.subzones / args.reps)
    print(f"  Distance bands: {num_bands}  "
          f"({args.min_dist}m → "
          f"{args.min_dist + num_bands * args.subzone_width}m from river)")

    centres = build_subzone_centres(
        zone['zone_geom'], zone['river_geom'],
        args.subzones, args.reps, args.subzone_width, args.min_dist,
    )
    print(f"  Subzone centres placed: {len(centres)}")

    if not centres:
        print("  No valid subzone positions found — check zone size vs distance params.")
        return None

    with psycopg2.connect(CONN_STRING) as _conn:
        agri_id, topo_id = ensure_data_sources(_conn)
        dataset_id = create_dataset(
            _conn, zone['zone_id'], args.subzones, args.reps,
            args.subzone_width, args.min_dist, agri_id, topo_id,
        )
        _conn.commit()

    print(f"  Dataset ID: {dataset_id}")

    all_pixel_coords = []
    for i, centre in enumerate(centres):
        subzone_wgs = subzone_box_wgs(centre, args.subzone_width)
        print(f"  Subzone {i+1}/{len(centres)} "
              f"centre=({centre.y:.5f}, {centre.x:.5f}) ... ",
              end='', flush=True)
        pixel_coords = sample_agri_pixels(subzone_wgs)
        if args.max_pixels and len(pixel_coords) > args.max_pixels:
            pixel_coords = random.sample(pixel_coords, args.max_pixels)
            print(f"{len(pixel_coords)} agri pixels (sampled from more, max-pixels limit)")
        else:
            print(f"{len(pixel_coords)} agri pixels")
        all_pixel_coords.extend(pixel_coords)

    if all_pixel_coords:
        with psycopg2.connect(CONN_STRING) as _conn:
            insert_pixels(_conn, all_pixel_coords, dataset_id, zone['zone_id'], agri_id)

    print(f"  {len(all_pixel_coords)} pixels written to pixels_static")

    print("  Populating terrain metrics ...")
    with psycopg2.connect(CONN_STRING) as _conn:
        populate_terrain_metrics(_conn, dataset_id, topo_id)

    return dataset_id


def main():
    parser = argparse.ArgumentParser(
        description="Sample agricultural pixels into pixels_static for study zones."
    )

    zone_group = parser.add_mutually_exclusive_group(required=False)
    zone_group.add_argument(
        "--zone-id",
        help="Process a single study_zones.zone_id",
    )
    zone_group.add_argument(
        "--zone-pattern",
        help="POSIX regex: process all zones with SUCCESS floods matching zone_id "
             "(e.g. 'initial' to match every zone_id containing 'initial')",
    )

    parser.add_argument("--subzones",      type=int, default=12,
                        help="Total subzone squares to place (default: 12)")
    parser.add_argument("--reps",          type=int, default=2,
                        help="Random placements per distance band (default: 2)")
    parser.add_argument("--subzone-width", type=int, default=500,
                        help="Subzone square side length in metres (default: 500)")
    parser.add_argument("--min-dist",      type=int, default=100,
                        help="Minimum distance from river in metres (default: 100)")
    parser.add_argument("--max-pixels",    type=int, default=500,
                        help="Max pixels to keep per subzone, randomly sampled (0 = no limit, default: 500)")
    parser.add_argument("--seed",          type=int, default=None,
                        help="Random seed for reproducibility (optional)")
    parser.add_argument("--test",          action="store_true",
                        help="Process only the first qualifying subzone per zone")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    if args.test:
        args.subzones = 4
        args.reps = 2
        print("TEST mode: overriding to 4 subzones, 2 reps (2 distance bands)")

    print(f"Subzones      : {args.subzones}  (reps={args.reps} per band)")
    print(f"Subzone width : {args.subzone_width} m")
    print(f"Min distance  : {args.min_dist} m from river")
    print(f"Seed          : {args.seed}")
    print(f"TEST          : {args.test}\n")

    ee.Initialize(project='foundation-flood')

    try:
        with psycopg2.connect(CONN_STRING) as conn:
            ensure_schema(conn)
            if args.zone_id:
                zones = [load_zone(conn, args.zone_id)]
            elif args.zone_pattern:
                zones = load_zones_for_pattern(conn, args.zone_pattern)
                print(f"Zones matching '{args.zone_pattern}' with SUCCESS floods: "
                      f"{len(zones)}")
            else:
                zones = load_zones_for_pattern(conn, '.*')
                print(f"No filter — all zones with SUCCESS floods: {len(zones)}")

        if not zones:
            print("No zones to process.")
            sys.exit(0)

        if args.test and len(zones) > 1:
            print(f"TEST mode: limiting to first zone of {len(zones)} matching.\n")
            zones = zones[:1]

        for zone_idx, zone in enumerate(zones):
            print(f"\n[{zone_idx + 1}/{len(zones)}] Zone: {zone['zone_id']}")
            dataset_id = process_zone(zone, args)
            if dataset_id is None:
                print(f"  Skipped (no valid subzone positions).")

        print("\nDone.")

    except (psycopg2.Error, ValueError) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
