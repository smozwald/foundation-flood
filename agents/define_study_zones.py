#!/usr/bin/env python3
"""
define_study_zones.py — Create or update study zones for Pakistan stations.

Reads discharge_stations rows that have river_id populated and upserts a
square study zone centred on each station into study_zones. No GEE needed —
rivers must already be in the rivers table.

Usage:
    python agents/define_study_zones.py [--zone-size METRES] [--zone-set NAME] [--test]
"""

import argparse
import os
import sys

import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv()

CONN_STRING = os.environ.get("SUPABASE_CONN_STRING")
if not CONN_STRING:
    print("ERROR: SUPABASE_CONN_STRING not set.")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Upsert study zones for Pakistan stations with linked rivers."
    )
    parser.add_argument("--zone-size", type=int, default=10_000,
                        help="Full side length in metres (default: 10000)")
    parser.add_argument("--zone-set", default="initial",
                        help="Zone set label, e.g. 'initial', '5km' (default: initial)")
    parser.add_argument("--test", action="store_true",
                        help="Process only the first matching station")
    args = parser.parse_args()

    half = args.zone_size / 2
    zone_label = (f"{args.zone_size // 1000}km"
                  if args.zone_size >= 1000 else f"{args.zone_size}m")

    print(f"Zone size  : {args.zone_size:,} m ({zone_label} square)")
    print(f"Zone set   : '{args.zone_set}'")
    print(f"TEST       : {args.test}")

    try:
        with psycopg2.connect(CONN_STRING) as conn:
            matched_df = pd.read_sql("""
                SELECT
                    ds.station_id::text,
                    ds.dfo_station_id,
                    ds.station_name,
                    ds.river_id::text
                FROM discharge_stations ds
                WHERE ds.river_id IS NOT NULL
                  AND ds.dfo_station_id != '000103'
                ORDER BY ds.station_name
            """, conn)

            if matched_df.empty:
                print("No stations with linked rivers found. Run HydroSHEDS extraction first.")
                sys.exit(1)

            if args.test:
                matched_df = matched_df.head(1)
                print(f"TEST mode: 1 station ({matched_df.iloc[0]['station_name']})\n")
            else:
                print(f"Stations   : {len(matched_df)}\n")

            with conn.cursor() as cur:
                for _, row in matched_df.iterrows():
                    zone_id = f"{row['dfo_station_id']}_{args.zone_set}"
                    cur.execute("""
                        INSERT INTO study_zones (
                            zone_id, station_id, river_id,
                            geom, centroid,
                            zone_set, position, zone_size_m,
                            generation_method, distance_m_along_river
                        )
                        SELECT
                            %(zone_id)s,
                            %(station_id)s::uuid,
                            %(river_id)s::uuid,
                            ST_Transform(
                                ST_MakeEnvelope(
                                    ST_X(ST_Transform(ds.geom::geometry, 32642)) - %(half)s,
                                    ST_Y(ST_Transform(ds.geom::geometry, 32642)) - %(half)s,
                                    ST_X(ST_Transform(ds.geom::geometry, 32642)) + %(half)s,
                                    ST_Y(ST_Transform(ds.geom::geometry, 32642)) + %(half)s,
                                    32642
                                ),
                                4326
                            ),
                            ds.geom::geometry,
                            %(zone_set)s,
                            0,
                            %(zone_size_m)s,
                            'centred_on_station',
                            ST_LineLocatePoint(r.geom::geometry,
                                ST_ClosestPoint(r.geom::geometry, ds.geom::geometry))
                            * ST_Length(r.geom::geography)
                        FROM discharge_stations ds, rivers r
                        WHERE ds.station_id = %(station_id)s::uuid
                          AND r.river_id    = %(river_id)s::uuid
                        ON CONFLICT (zone_id) DO UPDATE
                            SET geom                   = EXCLUDED.geom,
                                centroid               = EXCLUDED.centroid,
                                zone_set               = EXCLUDED.zone_set,
                                zone_size_m            = EXCLUDED.zone_size_m,
                                distance_m_along_river = EXCLUDED.distance_m_along_river
                    """, {
                        "zone_id":     zone_id,
                        "station_id":  row["station_id"],
                        "river_id":    row["river_id"],
                        "half":        half,
                        "zone_set":    args.zone_set,
                        "zone_size_m": args.zone_size,
                    })
                    print(f"  Upserted: {zone_id}")

            conn.commit()
            print(f"\nDone: {len(matched_df)} zones written to study_zones.")

    except psycopg2.Error as exc:
        print(f"Database error: {exc}")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
