#!/usr/bin/env python3
"""
db_setup.py — Database schema checker and applier for the flood risk project.

Usage:
    python agents/db_setup.py           # apply missing schema, then verify
    python agents/db_setup.py --check   # read-only check; exit 1 if issues found
"""

import argparse
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

CONN_STRING = os.environ.get("SUPABASE_CONN_STRING")
if not CONN_STRING:
    print("ERROR: SUPABASE_CONN_STRING is not set.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Schema definition
# ---------------------------------------------------------------------------

EXTENSIONS = ["postgis", "vector", "uuid-ossp"]

# Each table is (table_name, create_sql).
# Tables are listed in dependency order to satisfy FK constraints.
# CREATE TABLE IF NOT EXISTS keeps this idempotent.
TABLES = [
    (
        "data_sources",
        """
        CREATE TABLE IF NOT EXISTS data_sources (
            source_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            product_name     text NOT NULL,
            methodology_desc text,
            resolution_m     float,
            version_tag      text
        )
        """,
    ),
    (
        "rivers",
        """
        CREATE TABLE IF NOT EXISTS rivers (
            river_id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            river_name               text NOT NULL,
            geom                     geometry(LineString, 4326),
            river_source_metadata_id uuid REFERENCES data_sources(source_id)
        )
        """,
    ),
    (
        "study_zones",
        """
        CREATE TABLE IF NOT EXISTS study_zones (
            zone_id                text PRIMARY KEY,
            river_id               uuid REFERENCES rivers(river_id),
            station_id             uuid,
            geom                   geometry(Polygon, 4326),
            centroid               geometry(Point, 4326),
            zone_set               text,
            position               smallint,
            zone_size_m            integer,
            generation_method      text,
            distance_m_along_river float,
            generated_at           timestamptz DEFAULT now()
        )
        """,
    ),
    (
        "pixels_static",
        """
        CREATE TABLE IF NOT EXISTS pixels_static (
            pixel_id         text PRIMARY KEY,
            zone_id          text REFERENCES study_zones(zone_id),
            is_agri          boolean,
            agri_metadata_id uuid REFERENCES data_sources(source_id),
            elevation        float,
            slope            float,
            twi              float,
            spi              float,
            curvature        float,
            dist_to_river_m  float,
            topo_metadata_id uuid REFERENCES data_sources(source_id)
        )
        """,
    ),
    (
        "ts_sentinel1",
        """
        CREATE TABLE IF NOT EXISTS ts_sentinel1 (
            pixel_id        text REFERENCES pixels_static(pixel_id),
            obs_date        date NOT NULL,
            vv              float,
            vh              float,
            incidence_angle float,
            metadata        jsonb,
            PRIMARY KEY (pixel_id, obs_date)
        )
        """,
    ),
    (
        "ts_sentinel2",
        """
        CREATE TABLE IF NOT EXISTS ts_sentinel2 (
            pixel_id   text REFERENCES pixels_static(pixel_id),
            obs_date   date NOT NULL,
            b2         float,
            b3         float,
            b4         float,
            b5         float,
            b6         float,
            b7         float,
            b8         float,
            b8a        float,
            b11        float,
            b12        float,
            cloud_prob float,
            metadata   jsonb,
            PRIMARY KEY (pixel_id, obs_date)
        )
        """,
    ),
    (
        "ts_meteo",
        """
        CREATE TABLE IF NOT EXISTS ts_meteo (
            pixel_id          text REFERENCES pixels_static(pixel_id),
            obs_date          date NOT NULL,
            precip_mm         float,
            temp_max          float,
            temp_min          float,
            source_station_id text,
            PRIMARY KEY (pixel_id, obs_date)
        )
        """,
    ),
    (
        "labels_flooding",
        """
        CREATE TABLE IF NOT EXISTS labels_flooding (
            pixel_id           text REFERENCES pixels_static(pixel_id),
            obs_date           date NOT NULL,
            is_flooded         boolean,
            algorithm_version  text,
            method_metadata_id uuid REFERENCES data_sources(source_id),
            event_id           uuid,
            PRIMARY KEY (pixel_id, obs_date)
        )
        """,
    ),
    (
        "pixel_embeddings",
        """
        CREATE TABLE IF NOT EXISTS pixel_embeddings (
            pixel_id     text REFERENCES pixels_static(pixel_id),
            window_start date NOT NULL,
            model_name   text NOT NULL,
            embedding    vector(128),
            PRIMARY KEY (pixel_id, window_start, model_name)
        )
        """,
    ),
    (
        "discharge_stations",
        """
        CREATE TABLE IF NOT EXISTS discharge_stations (
            station_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            dfo_station_id text UNIQUE NOT NULL,
            station_name   text,
            geom           geometry(Point, 4326) NOT NULL,
            zone_id        text REFERENCES study_zones(zone_id),
            river_id       uuid REFERENCES rivers(river_id),
            source_id      uuid REFERENCES data_sources(source_id),
            record_start   date,
            record_end     date
        )
        """,
    ),
    (
        "discharge_ts",
        """
        CREATE TABLE IF NOT EXISTS discharge_ts (
            station_id                  uuid REFERENCES discharge_stations(station_id),
            obs_date                    date NOT NULL,
            discharge_m3s               float,
            discharge_anomaly_pct       float,
            threshold_exceeded_category smallint CHECK (threshold_exceeded_category BETWEEN 1 AND 5),
            qc_flag                     text,
            source_id                   uuid REFERENCES data_sources(source_id),
            PRIMARY KEY (station_id, obs_date)
        )
        """,
    ),
    (
        "flood_thresholds",
        """
        CREATE TABLE IF NOT EXISTS flood_thresholds (
            threshold_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            station_id              uuid REFERENCES discharge_stations(station_id) NOT NULL,
            category                smallint NOT NULL CHECK (category BETWEEN 1 AND 5),
            return_period_label     text NOT NULL,
            discharge_threshold_m3s float NOT NULL,
            derived_from            text DEFAULT 'DFO_station_page',
            valid_from              date,
            valid_to                date,
            UNIQUE (station_id, category)
        )
        """,
    ),
    (
        "flood_events",
        """
        CREATE TABLE IF NOT EXISTS flood_events (
            event_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            station_id         uuid REFERENCES discharge_stations(station_id) NOT NULL,
            river_id           uuid REFERENCES rivers(river_id),
            flood_start        date NOT NULL,
            flood_end          date NOT NULL,
            duration_days      integer,
            peak_discharge_m3s float,
            peak_date          date,
            max_category       smallint CHECK (max_category BETWEEN 1 AND 5),
            dfo_archive_id     text,
            detection_method   text DEFAULT 'threshold_exceedance',
            notes              text,
            CONSTRAINT valid_dates CHECK (flood_end >= flood_start)
        )
        """,
    ),
]

# Columns to ensure exist per table: (table, column, type_definition).
# These handle schema drift on existing tables without recreating them.
# No catchment_id entries anywhere.
COLUMNS = [
    ("data_sources", "source_id",        "uuid DEFAULT gen_random_uuid()"),
    ("data_sources", "product_name",     "text"),
    ("data_sources", "methodology_desc", "text"),
    ("data_sources", "resolution_m",     "float"),
    ("data_sources", "version_tag",      "text"),

    ("rivers", "river_id",                 "uuid DEFAULT gen_random_uuid()"),
    ("rivers", "river_name",               "text"),
    ("rivers", "geom",                     "geometry(LineString, 4326)"),
    ("rivers", "river_source_metadata_id", "uuid"),

    ("study_zones", "zone_id",               "text"),
    ("study_zones", "river_id",              "uuid"),
    ("study_zones", "station_id",            "uuid"),
    ("study_zones", "geom",                  "geometry(Polygon, 4326)"),
    ("study_zones", "centroid",              "geometry(Point, 4326)"),
    ("study_zones", "zone_set",              "text"),
    ("study_zones", "position",              "smallint"),
    ("study_zones", "zone_size_m",           "integer"),
    ("study_zones", "generation_method",     "text"),
    ("study_zones", "distance_m_along_river","float"),
    ("study_zones", "generated_at",          "timestamptz DEFAULT now()"),

    ("pixels_static", "pixel_id",         "text"),
    ("pixels_static", "zone_id",          "text"),
    ("pixels_static", "is_agri",          "boolean"),
    ("pixels_static", "agri_metadata_id", "uuid"),
    ("pixels_static", "elevation",        "float"),
    ("pixels_static", "slope",            "float"),
    ("pixels_static", "twi",              "float"),
    ("pixels_static", "spi",              "float"),
    ("pixels_static", "curvature",        "float"),
    ("pixels_static", "dist_to_river_m",  "float"),
    ("pixels_static", "topo_metadata_id", "uuid"),

    ("ts_sentinel1", "pixel_id",        "text"),
    ("ts_sentinel1", "obs_date",        "date"),
    ("ts_sentinel1", "vv",              "float"),
    ("ts_sentinel1", "vh",              "float"),
    ("ts_sentinel1", "incidence_angle", "float"),
    ("ts_sentinel1", "metadata",        "jsonb"),

    ("ts_sentinel2", "pixel_id",   "text"),
    ("ts_sentinel2", "obs_date",   "date"),
    ("ts_sentinel2", "b2",         "float"),
    ("ts_sentinel2", "b3",         "float"),
    ("ts_sentinel2", "b4",         "float"),
    ("ts_sentinel2", "b5",         "float"),
    ("ts_sentinel2", "b6",         "float"),
    ("ts_sentinel2", "b7",         "float"),
    ("ts_sentinel2", "b8",         "float"),
    ("ts_sentinel2", "b8a",        "float"),
    ("ts_sentinel2", "b11",        "float"),
    ("ts_sentinel2", "b12",        "float"),
    ("ts_sentinel2", "cloud_prob", "float"),
    ("ts_sentinel2", "metadata",   "jsonb"),

    ("ts_meteo", "pixel_id",          "text"),
    ("ts_meteo", "obs_date",          "date"),
    ("ts_meteo", "precip_mm",         "float"),
    ("ts_meteo", "temp_max",          "float"),
    ("ts_meteo", "temp_min",          "float"),
    ("ts_meteo", "source_station_id", "text"),

    ("labels_flooding", "pixel_id",           "text"),
    ("labels_flooding", "obs_date",           "date"),
    ("labels_flooding", "is_flooded",         "boolean"),
    ("labels_flooding", "algorithm_version",  "text"),
    ("labels_flooding", "method_metadata_id", "uuid"),
    ("labels_flooding", "event_id",           "uuid"),

    ("pixel_embeddings", "pixel_id",     "text"),
    ("pixel_embeddings", "window_start", "date"),
    ("pixel_embeddings", "model_name",   "text"),
    ("pixel_embeddings", "embedding",    "vector(128)"),

    ("discharge_stations", "station_id",     "uuid DEFAULT gen_random_uuid()"),
    ("discharge_stations", "dfo_station_id", "text"),
    ("discharge_stations", "station_name",   "text"),
    ("discharge_stations", "geom",           "geometry(Point, 4326)"),
    ("discharge_stations", "zone_id",        "text"),
    ("discharge_stations", "river_id",       "uuid"),
    ("discharge_stations", "source_id",      "uuid"),
    ("discharge_stations", "record_start",   "date"),
    ("discharge_stations", "record_end",     "date"),

    ("discharge_ts", "station_id",                  "uuid"),
    ("discharge_ts", "obs_date",                    "date"),
    ("discharge_ts", "discharge_m3s",               "float"),
    ("discharge_ts", "discharge_anomaly_pct",       "float"),
    ("discharge_ts", "threshold_exceeded_category", "smallint"),
    ("discharge_ts", "qc_flag",                     "text"),
    ("discharge_ts", "source_id",                   "uuid"),

    ("flood_thresholds", "threshold_id",            "uuid DEFAULT gen_random_uuid()"),
    ("flood_thresholds", "station_id",              "uuid"),
    ("flood_thresholds", "category",                "smallint"),
    ("flood_thresholds", "return_period_label",     "text"),
    ("flood_thresholds", "discharge_threshold_m3s", "float"),
    ("flood_thresholds", "derived_from",            "text DEFAULT 'DFO_station_page'"),
    ("flood_thresholds", "valid_from",              "date"),
    ("flood_thresholds", "valid_to",                "date"),

    ("flood_events", "event_id",           "uuid DEFAULT gen_random_uuid()"),
    ("flood_events", "station_id",         "uuid"),
    ("flood_events", "river_id",           "uuid"),
    ("flood_events", "flood_start",        "date"),
    ("flood_events", "flood_end",          "date"),
    ("flood_events", "duration_days",      "integer"),
    ("flood_events", "peak_discharge_m3s", "float"),
    ("flood_events", "peak_date",          "date"),
    ("flood_events", "max_category",       "smallint"),
    ("flood_events", "dfo_archive_id",     "text"),
    ("flood_events", "detection_method",   "text DEFAULT 'threshold_exceedance'"),
    ("flood_events", "notes",              "text"),
]

# Indexes: (index_name, table, create_sql)
INDEXES = [
    (
        "study_zones_geom_gist",
        "study_zones",
        "CREATE INDEX IF NOT EXISTS study_zones_geom_gist ON study_zones USING GIST (geom)",
    ),
    (
        "study_zones_river_id_btree",
        "study_zones",
        "CREATE INDEX IF NOT EXISTS study_zones_river_id_btree ON study_zones (river_id)",
    ),
    (
        "pixels_static_zone_id_btree",
        "pixels_static",
        "CREATE INDEX IF NOT EXISTS pixels_static_zone_id_btree ON pixels_static (zone_id)",
    ),
    (
        "discharge_stations_geom_gist",
        "discharge_stations",
        "CREATE INDEX IF NOT EXISTS discharge_stations_geom_gist ON discharge_stations USING GIST (geom)",
    ),
    (
        "discharge_ts_station_obs_date_desc",
        "discharge_ts",
        "CREATE INDEX IF NOT EXISTS discharge_ts_station_obs_date_desc ON discharge_ts (station_id, obs_date DESC)",
    ),
    (
        "discharge_ts_exceeded_partial",
        "discharge_ts",
        "CREATE INDEX IF NOT EXISTS discharge_ts_exceeded_partial ON discharge_ts (station_id) WHERE threshold_exceeded_category IS NOT NULL",
    ),
    (
        "flood_events_station_id_btree",
        "flood_events",
        "CREATE INDEX IF NOT EXISTS flood_events_station_id_btree ON flood_events (station_id)",
    ),
    (
        "flood_events_dates_btree",
        "flood_events",
        "CREATE INDEX IF NOT EXISTS flood_events_dates_btree ON flood_events (flood_start, flood_end)",
    ),
]

# Deferred FK constraints: (constraint_name, table, column, ref_table, ref_column)
# These cannot be wired at CREATE TABLE time due to circular / ordering issues.
DEFERRED_FKS = [
    (
        "labels_flooding_event_id_fkey",
        "labels_flooding",
        "event_id",
        "flood_events",
        "event_id",
    ),
    (
        "study_zones_station_id_fkey",
        "study_zones",
        "station_id",
        "discharge_stations",
        "station_id",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_existing_extensions(cur):
    cur.execute("SELECT extname FROM pg_extension")
    return {row[0] for row in cur.fetchall()}


def get_existing_tables(cur):
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public'"
    )
    return {row[0] for row in cur.fetchall()}


def get_existing_columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %(table)s",
        {"table": table},
    )
    return {row[0] for row in cur.fetchall()}


def get_existing_constraints(cur):
    cur.execute(
        "SELECT constraint_name FROM information_schema.table_constraints "
        "WHERE table_schema = 'public'"
    )
    return {row[0] for row in cur.fetchall()}


def get_existing_indexes(cur):
    cur.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
    )
    return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Check logic — returns list of issue strings
# ---------------------------------------------------------------------------

def collect_issues(cur):
    issues = []

    existing_ext = get_existing_extensions(cur)
    for ext in EXTENSIONS:
        if ext not in existing_ext:
            issues.append(f"Missing extension: {ext}")

    existing_tables = get_existing_tables(cur)
    for table_name, _ in TABLES:
        if table_name not in existing_tables:
            issues.append(f"Missing table: {table_name}")
            continue
        # Table exists — check columns
        existing_cols = get_existing_columns(cur, table_name)
        for tbl, col, _ in COLUMNS:
            if tbl == table_name and col not in existing_cols:
                issues.append(f"Missing column: {table_name}.{col}")

    existing_indexes = get_existing_indexes(cur)
    for idx_name, _, _ in INDEXES:
        if idx_name not in existing_indexes:
            issues.append(f"Missing index: {idx_name}")

    existing_constraints = get_existing_constraints(cur)
    for constraint_name, table, col, ref_table, ref_col in DEFERRED_FKS:
        if constraint_name not in existing_constraints:
            issues.append(f"Missing FK constraint: {constraint_name}")

    return issues


# ---------------------------------------------------------------------------
# Apply logic
# ---------------------------------------------------------------------------

def apply_schema(conn):
    with conn.cursor() as cur:
        # Extensions
        existing_ext = get_existing_extensions(cur)
        for ext in EXTENSIONS:
            if ext not in existing_ext:
                print(f"Creating extension: {ext}")
                # ext is a schema identifier, not a user value — safe to interpolate
                cur.execute('CREATE EXTENSION IF NOT EXISTS "%s"' % ext)
        conn.commit()

        # Tables
        existing_tables = get_existing_tables(cur)
        for table_name, create_sql in TABLES:
            if table_name not in existing_tables:
                print(f"Creating table: {table_name}")
            cur.execute(create_sql)
        conn.commit()

        # Refresh table list after creates
        existing_tables = get_existing_tables(cur)

        # Columns (for tables that already existed before this run)
        for table_name, col, col_type in COLUMNS:
            if table_name not in existing_tables:
                continue
            existing_cols = get_existing_columns(cur, table_name)
            if col not in existing_cols:
                print(f"Adding column: {table_name}.{col}")
                # col and col_type are schema identifiers — safe literals
                cur.execute(
                    "ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s"
                    % (table_name, col, col_type)
                )
        conn.commit()

        # Indexes
        existing_indexes = get_existing_indexes(cur)
        for idx_name, _, idx_sql in INDEXES:
            if idx_name not in existing_indexes:
                print(f"Creating index: {idx_name}")
            cur.execute(idx_sql)
        conn.commit()

        # Deferred FK constraints
        existing_constraints = get_existing_constraints(cur)
        for constraint_name, table, col, ref_table, ref_col in DEFERRED_FKS:
            if constraint_name not in existing_constraints:
                print(f"Adding FK constraint: {constraint_name}")
                cur.execute(
                    "ALTER TABLE %s ADD CONSTRAINT %s "
                    "FOREIGN KEY (%s) REFERENCES %s(%s)"
                    % (table, constraint_name, col, ref_table, ref_col)
                )
        conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Check or apply the flood-risk database schema."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Read-only mode: report issues and exit 1 if any found.",
    )
    args = parser.parse_args()

    try:
        with psycopg2.connect(CONN_STRING) as conn:
            if args.check:
                print("Running schema check (read-only)...")
                with conn.cursor() as cur:
                    issues = collect_issues(cur)
                if issues:
                    print(f"Found {len(issues)} issue(s):")
                    for issue in issues:
                        print(f"  - {issue}")
                    sys.exit(1)
                else:
                    print("Schema OK")
                    sys.exit(0)
            else:
                print("Applying schema...")
                apply_schema(conn)
                print("Schema applied. Re-checking...")
                with conn.cursor() as cur:
                    issues = collect_issues(cur)
                if issues:
                    print(f"WARNING: {len(issues)} issue(s) remain after apply:")
                    for issue in issues:
                        print(f"  - {issue}")
                    sys.exit(1)
                else:
                    print("Schema OK")
                    sys.exit(0)

    except psycopg2.Error as exc:
        print(f"Database error: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"Unexpected error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
