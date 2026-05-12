#!/usr/bin/env python3
"""
select_study_pixels.py — Sample Sentinel-grid-aligned agricultural pixels per study zone.

Places subzone squares at increasing distances from the river in distance bands of
subzone_width_m, starting at min_dist_m. Picks `reps` random positions per band
until `subzones` total squares are placed. Agricultural pixels (ESA WorldCover
class 40) within each subzone are written to pixels_static.

Creates study_zone_dataset (if absent) and adds geom + study_zone_dataset_id
columns to pixels_static (if absent) before inserting.

Usage:
    python agents/select_study_pixels.py --zone-id ZONE_ID [options]

Options:
    --zone-id TEXT          study_zones.zone_id to process (required)
    --subzones INT          total subzone squares to place (default: 12)
    --reps INT              random positions per distance band (default: 2)
    --subzone-width INT     side length of each subzone square in metres (default: 500)
    --min-dist INT          minimum distance from river in metres (default: 100)
    --seed INT              random seed for reproducibility (optional)
    --test                  process only the first qualifying subzone
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

UTM42N = 32642  # EPSG for Pakistan (UTM zone 42N)

DDL_STUDY_ZONE_DATASET = """
CREATE TABLE IF NOT EXISTS study_zone_dataset (
    dataset_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    zone_id         text NOT NULL REFERENCES study_zones(zone_id),
    subzones        integer NOT NULL,
    reps            integer NOT NULL,
    subzone_width_m integer NOT NULL,
    min_dist_m      integer NOT NULL,
    created_at      timestamptz DEFAULT now()
)
"""


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(DDL_STUDY_ZONE_DATASET)
        cur.execute("""
            ALTER TABLE pixels_static
                ADD COLUMN IF NOT EXISTS study_zone_dataset_id uuid
                    REFERENCES study_zone_dataset(dataset_id),
                ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326)
        """)
    conn.commit()


def load_zone(conn, zone_id):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                sz.zone_id,
                sz.station_id::text,
                sz.zone_size_m,
                ST_AsGeoJSON(sz.geom)     AS zone_geom_json,
                ST_AsGeoJSON(r.geom)      AS river_geom_json
            FROM study_zones sz
            JOIN rivers r ON r.river_id = sz.river_id
            WHERE sz.zone_id = %(zone_id)s
        """, {'zone_id': zone_id})
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"zone_id '{zone_id}' not found in study_zones "
                         "(or has no linked river).")
    return {
        'zone_id':        row[0],
        'station_id':     row[1],
        'zone_size_m':    row[2],
        'zone_geom':      shape(json.loads(row[3])),
        'river_geom':     shape(json.loads(row[4])),
    }


def create_dataset(conn, zone_id, subzones, reps, subzone_width, min_dist):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO study_zone_dataset (zone_id, subzones, reps, subzone_width_m, min_dist_m)
            VALUES (%(zone_id)s, %(subzones)s, %(reps)s, %(subzone_width_m)s, %(min_dist_m)s)
            RETURNING dataset_id::text
        """, {'zone_id': zone_id, 'subzones': subzones, 'reps': reps,
              'subzone_width_m': subzone_width, 'min_dist_m': min_dist})
        return cur.fetchone()[0]


def build_subzone_centres(zone_geom, river_geom, subzones, reps, subzone_width, min_dist):
    """
    Generate subzone centre points in WGS84.

    Distance bands start at min_dist from the river, each band is subzone_width wide.
    Within each band, pick `reps` random positions by rejection-sampling within the
    band-annulus intersected with the study zone. Continues until `subzones` total
    positions are found or all bands within the zone are exhausted.
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

        # Intersect with zone and shrink inward by half subzone width so the
        # square fits entirely within the zone when centred on the sampled point.
        valid = zone_utm.buffer(-half_sw).intersection(annulus)

        if valid.is_empty:
            continue

        bounds     = valid.bounds
        band_found = 0
        attempts   = 0
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

    # Convert back to WGS84
    return [
        shapely_transform(to_wgs, pt)
        for pt in centres
    ]


def subzone_box_wgs(centre_wgs, subzone_width):
    """Create a subzone_width x subzone_width square around centre_wgs (WGS84)."""
    to_utm = Transformer.from_crs(4326, UTM42N, always_xy=True).transform
    to_wgs = Transformer.from_crs(UTM42N, 4326, always_xy=True).transform
    cx, cy  = to_utm(centre_wgs.x, centre_wgs.y)
    half    = subzone_width / 2
    sq_utm  = box(cx - half, cy - half, cx + half, cy + half)
    return shapely_transform(to_wgs, sq_utm)


def sample_agri_pixels(subzone_wgs):
    """
    Return list of (lon, lat) for ESA WorldCover cropland pixels (class 40)
    within subzone_wgs, sampled at 10 m scale.
    """
    coords   = list(subzone_wgs.exterior.coords)
    ee_geom  = ee.Geometry.Polygon(coords)
    worldcover = ee.ImageCollection('ESA/WorldCover/v200').first().select('Map')
    samples  = (worldcover.eq(40).selfMask()
                .sample(region=ee_geom, scale=10, geometries=True, tileScale=4))
    fc_info  = samples.limit(10_000).getInfo()
    return [feat['geometry']['coordinates'] for feat in fc_info.get('features', [])]


def pixel_id_from_lonlat(dataset_id_short, lon, lat):
    to_utm = Transformer.from_crs(4326, UTM42N, always_xy=True).transform
    e, n   = to_utm(lon, lat)
    e10    = int(round(e / 10))
    n10    = int(round(n / 10))
    return f"{dataset_id_short}_{e10}_{n10}"


def insert_pixels(conn, pixels, dataset_id):
    dataset_id_short = dataset_id.replace('-', '')[:12]
    with conn.cursor() as cur:
        for lon, lat in pixels:
            pixel_id = pixel_id_from_lonlat(dataset_id_short, lon, lat)
            cur.execute("""
                INSERT INTO pixels_static (pixel_id, study_zone_dataset_id, is_agri, geom)
                VALUES (
                    %(pixel_id)s,
                    %(dataset_id)s::uuid,
                    true,
                    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)
                )
                ON CONFLICT (pixel_id) DO NOTHING
            """, {'pixel_id': pixel_id, 'dataset_id': dataset_id,
                  'lon': lon, 'lat': lat})
    conn.commit()


def main():
    parser = argparse.ArgumentParser(
        description="Sample agricultural pixels into pixels_static for a study zone."
    )
    parser.add_argument("--zone-id",       required=True,
                        help="study_zones.zone_id to process")
    parser.add_argument("--subzones",      type=int, default=12,
                        help="Total subzone squares to place (default: 12)")
    parser.add_argument("--reps",          type=int, default=2,
                        help="Random placements per distance band (default: 2)")
    parser.add_argument("--subzone-width", type=int, default=500,
                        help="Subzone square side length in metres (default: 500)")
    parser.add_argument("--min-dist",      type=int, default=100,
                        help="Minimum distance from river in metres (default: 100)")
    parser.add_argument("--seed",          type=int, default=None,
                        help="Random seed for reproducibility (optional)")
    parser.add_argument("--test",          action="store_true",
                        help="Process only the first qualifying subzone then exit")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    print(f"Zone ID       : {args.zone_id}")
    print(f"Subzones      : {args.subzones}  (reps={args.reps} per band)")
    print(f"Subzone width : {args.subzone_width} m")
    print(f"Min distance  : {args.min_dist} m from river")
    print(f"Seed          : {args.seed}")
    print(f"TEST          : {args.test}\n")

    ee.Initialize(project='foundation-flood')

    try:
        with psycopg2.connect(CONN_STRING) as conn:
            ensure_schema(conn)
            zone = load_zone(conn, args.zone_id)

        zone_w = zone['zone_size_m']
        zone_h = zone['zone_size_m']
        print(f"Study zone    : {zone_w} m × {zone_h} m")

        num_bands = math.ceil(args.subzones / args.reps)
        print(f"Distance bands: {num_bands}  "
              f"({args.min_dist}m → "
              f"{args.min_dist + num_bands * args.subzone_width}m from river)\n")

        centres = build_subzone_centres(
            zone['zone_geom'], zone['river_geom'],
            args.subzones, args.reps, args.subzone_width, args.min_dist,
        )
        print(f"Subzone centres placed: {len(centres)}")

        if not centres:
            print("No valid subzone positions found — check zone size vs distance params.")
            sys.exit(1)

        if args.test:
            centres = centres[:1]
            print("TEST mode: processing 1 subzone\n")

        with psycopg2.connect(CONN_STRING) as conn:
            dataset_id = create_dataset(
                conn, args.zone_id, args.subzones, args.reps,
                args.subzone_width, args.min_dist,
            )
            conn.commit()

        print(f"Dataset ID: {dataset_id}\n")

        total_pixels = 0
        for i, centre in enumerate(centres):
            subzone_wgs = subzone_box_wgs(centre, args.subzone_width)
            print(f"  Subzone {i+1}/{len(centres)} "
                  f"centre=({centre.y:.5f}, {centre.x:.5f}) ... ",
                  end='', flush=True)

            pixel_coords = sample_agri_pixels(subzone_wgs)
            print(f"{len(pixel_coords)} agri pixels")

            if pixel_coords:
                with psycopg2.connect(CONN_STRING) as conn:
                    insert_pixels(conn, pixel_coords, dataset_id)
                total_pixels += len(pixel_coords)

        print(f"\nDone: {total_pixels} pixels written to pixels_static "
              f"(dataset_id={dataset_id})")

    except (psycopg2.Error, ValueError) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
