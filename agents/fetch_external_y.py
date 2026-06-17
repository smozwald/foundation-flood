#!/usr/bin/env python3
"""
fetch_external_y.py — Phase 2 Step 1: External Y% flood hazard source.

Pull return-period flood hazard from GEE candidate products, compute the
zone-inundated fraction Y%(RP) per study zone, store in external_flood_hazard,
then sanity-check against observed flood fractions in zone_flood_analysis.

Sources
-------
- jrc : JRC/CEMS_GLOFAS/FloodHazard/v2_1
        Multi-band mosaic (RP10/20/50/75/100/200/500 depth in m).
        Band depth > 0 → inundated. Native scale ~93 m.
- floodhub : Google Flood Hub / GRRR — no public GEE inundation/RP product
        found for this region; recorded as limitation, skipped.

Gate
----
PASS : ≥1 source has a monotonic Y%(RP) curve with at least one RP giving
       Y% in [10, 60] (order-of-magnitude match to Aug-2025 Cat-5 ≈ 36.9%).
FAIL : no source meets the criteria → stop, record finding. Exit 0 (completed run).

Usage
-----
python agents/fetch_external_y.py [options]

Flags
-----
--source      {jrc|floodhub|all}  default: all
--zone-pattern  regex on zone_id   default: .*_initial
--zone-id       single zone_id (overrides --zone-pattern)
--test          first zone × jrc only; still writes to DB
--dry-run       compute + print, no DB inserts

Conventions (CLAUDE.md)
-----------------------
SUPABASE_CONN_STRING via .env; with psycopg2.connect; %(name)s params;
stdout progress. Exit 0 on success (GATE: FAIL is still exit 0); exit 1 on error.
"""

import argparse
import json
import os
import re
import sys

import ee
import psycopg2
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

GEE_PROJECT = "foundation-flood"
GEE_SA_KEY  = os.path.join(os.path.dirname(__file__), "..", "gcp-key.json")

CONN_STRING = os.environ.get("SUPABASE_CONN_STRING")
if not CONN_STRING:
    print("ERROR: SUPABASE_CONN_STRING not set.")
    sys.exit(1)

# JRC/CEMS_GLOFAS v2_1 — multi-band mosaic; all RP bands in each tile
JRC_ASSET   = "JRC/CEMS_GLOFAS/FloodHazard/v2_1"
JRC_RPS     = [10, 20, 50, 75, 100, 200, 500]
JRC_BANDS   = [f"RP{rp}_depth" for rp in JRC_RPS]  # depth in metres
JRC_SCALE_M = 92.77   # native resolution ~93 m
JRC_TILE_SCALE = 4    # avoids OOM on large zones

# Sanity gate thresholds
GATE_Y_MIN   = 10.0   # at least one RP must reach this Y% …
GATE_Y_MAX   = 60.0   # … but not exceed this (not all-flooded noise)
MONO_EPSILON = 0.001  # tolerance for monotonicity check (% units)

# Source metadata
SOURCES_META = {
    "jrc": {
        "product_name": "JRC/CEMS_GLOFAS/FloodHazard/v2_1",
        "methodology_desc": (
            "GloFAS return-period flood depth (v2.1). "
            "Y% = pixelArea-weighted fraction of zone where depth > 0 at each RP. "
            "No permanent-water masking applied (product excludes permanent water "
            "via permanent_water_class band but we sample raw depth bands). "
            "Sanity-checked vs zone_flood_analysis observed fractions; "
            "not basin-calibrated."
        ),
        "resolution_m": JRC_SCALE_M,
        "version_tag":  "v2_1",
    }
}

# ── DDL ───────────────────────────────────────────────────────────────────────

DDL_EXTERNAL_FLOOD_HAZARD = """
CREATE TABLE IF NOT EXISTS external_flood_hazard (
    zone_id            text             NOT NULL REFERENCES study_zones(zone_id),
    source_product     text             NOT NULL,
    return_period_yr   integer          NOT NULL,
    y_pct              double precision NOT NULL,
    native_scale_m     double precision,
    source_metadata_id uuid             REFERENCES data_sources(source_id),
    computed_at        timestamptz      DEFAULT now(),
    PRIMARY KEY (zone_id, source_product, return_period_yr)
)
"""

# ── GEE init ──────────────────────────────────────────────────────────────────

def init_gee():
    """Initialise Earth Engine; prefer service-account key, fall back to ADC."""
    key_path = os.path.abspath(GEE_SA_KEY)
    if os.path.exists(key_path):
        creds = ee.ServiceAccountCredentials(None, key_path)
        ee.Initialize(credentials=creds, project=GEE_PROJECT)
        print(f"GEE: initialised via service account ({os.path.basename(key_path)})")
    else:
        ee.Initialize(project=GEE_PROJECT)
        print("GEE: initialised via application default credentials")


# ── DB helpers ────────────────────────────────────────────────────────────────

def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(DDL_EXTERNAL_FLOOD_HAZARD)
    conn.commit()


def upsert_source(conn, meta: dict) -> str:
    """Upsert a data_sources row; return source_id."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO data_sources
                (product_name, methodology_desc, resolution_m, version_tag)
            VALUES
                (%(product_name)s, %(methodology_desc)s,
                 %(resolution_m)s,  %(version_tag)s)
            ON CONFLICT (product_name) DO UPDATE
                SET methodology_desc = EXCLUDED.methodology_desc,
                    resolution_m     = EXCLUDED.resolution_m,
                    version_tag      = EXCLUDED.version_tag
            RETURNING source_id
        """, meta)
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT source_id FROM data_sources WHERE product_name = %(pn)s",
                {"pn": meta["product_name"]},
            )
            row = cur.fetchone()
        conn.commit()
        return str(row[0])


def write_rows(conn, rows: list):
    """Upsert list of external_flood_hazard rows."""
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO external_flood_hazard
                (zone_id, source_product, return_period_yr,
                 y_pct, native_scale_m, source_metadata_id)
            VALUES
                (%(zone_id)s, %(source_product)s, %(return_period_yr)s,
                 %(y_pct)s,   %(native_scale_m)s, %(source_metadata_id)s::uuid)
            ON CONFLICT (zone_id, source_product, return_period_yr)
            DO UPDATE SET
                y_pct              = EXCLUDED.y_pct,
                native_scale_m     = EXCLUDED.native_scale_m,
                source_metadata_id = EXCLUDED.source_metadata_id,
                computed_at        = now()
        """, rows)
    conn.commit()


def load_zones(conn, pattern: str, zone_id_filter: str = None) -> list:
    """Return list of (zone_id, geom_json) matching pattern."""
    with conn.cursor() as cur:
        if zone_id_filter:
            cur.execute("""
                SELECT zone_id, ST_AsGeoJSON(geom)
                FROM study_zones
                WHERE zone_id = %(zid)s
                ORDER BY zone_id
            """, {"zid": zone_id_filter})
        else:
            cur.execute("""
                SELECT zone_id, ST_AsGeoJSON(geom)
                FROM study_zones
                ORDER BY zone_id
            """)
        rows = cur.fetchall()

    rx = re.compile(pattern)
    return [(z, g) for z, g in rows if rx.search(z)]


def load_observed(conn) -> dict:
    """
    Return {zone_id: (cat1_range, cat5_pct)} from zone_flood_analysis.
    cat1_range = (min_pct, max_pct) over Cat-1 SUCCESS rows.
    cat5_pct   = mean flooded_agri_pct for Cat-5 SUCCESS rows.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT zone_id, max_category, flooded_agri_pct
            FROM zone_flood_analysis
            WHERE status = 'SUCCESS'
              AND flooded_agri_pct IS NOT NULL
            ORDER BY zone_id, max_category
        """)
        rows = cur.fetchall()

    obs = {}
    for zone_id, cat, pct in rows:
        obs.setdefault(zone_id, {}).setdefault(cat, []).append(pct)
    return obs


# ── Computation ───────────────────────────────────────────────────────────────

def compute_jrc_curve(geom_ee: ee.Geometry) -> dict:
    """
    Compute Y%(RP) for each JRC return period over the given EE geometry.

    Returns {rp_int: y_pct_float}.

    Methodology:
      mask = RP_depth > 0
      Y% = pixelArea-weighted mean of mask over zone = inundated_area / zone_area
    unmask(0) fills nodata with 0 so nodata pixels count as not-inundated
    (conservative; nodata outside product extent is truly 0 inundation).
    """
    mosaic = ee.ImageCollection(JRC_ASSET).mosaic()

    # Denominator: total zone area in m²
    zone_area = (
        ee.Image.pixelArea()
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom_ee,
            scale=JRC_SCALE_M,
            tileScale=JRC_TILE_SCALE,
            maxPixels=1e9,
        )
        .getInfo()
        .get("area", None)
    )
    if not zone_area:
        raise RuntimeError("Could not compute zone area via pixelArea")

    result = {}
    for band, rp in zip(JRC_BANDS, JRC_RPS):
        depth_img  = mosaic.select(band).unmask(0)
        flood_mask = depth_img.gt(0).rename("flooded")
        flooded_area = (
            flood_mask.multiply(ee.Image.pixelArea())
            .reduceRegion(
                reducer=ee.Reducer.sum(),
                geometry=geom_ee,
                scale=JRC_SCALE_M,
                tileScale=JRC_TILE_SCALE,
                maxPixels=1e9,
            )
            .getInfo()
            .get("flooded", 0) or 0
        )
        result[rp] = round(flooded_area / zone_area * 100, 4)

    return result


# ── Sanity checks ─────────────────────────────────────────────────────────────

def is_monotonic(curve: dict, epsilon: float = MONO_EPSILON) -> bool:
    """Check that Y% is non-decreasing with RP (within epsilon tolerance)."""
    vals = [curve[rp] for rp in sorted(curve)]
    for i in range(1, len(vals)):
        if vals[i] < vals[i - 1] - epsilon:
            return False
    return True


def has_plausible_range(curve: dict) -> bool:
    """At least one RP gives Y% in [GATE_Y_MIN, GATE_Y_MAX]."""
    return any(GATE_Y_MIN <= v <= GATE_Y_MAX for v in curve.values())


def print_curve_vs_observed(zone_id: str, source: str, curve: dict, obs: dict):
    """Print the RP curve next to any observed fractions for this zone."""
    print(f"    RP curve ({source}):")
    zone_obs = obs.get(zone_id, {})
    for rp in sorted(curve):
        y = curve[rp]
        obs_notes = []
        for cat, pcts in zone_obs.items():
            # Just note what we observed; no RP→category mapping
            obs_notes.append(f"Cat-{cat}: {min(pcts):.1f}–{max(pcts):.1f}%"
                             if len(pcts) > 1 else f"Cat-{cat}: {pcts[0]:.1f}%")
        obs_str = "  [obs " + ", ".join(obs_notes) + "]" if obs_notes else ""
        print(f"      RP{rp:4d}: {y:6.2f}%{obs_str if rp == sorted(curve)[-1] else ''}")
    # Print all observed on summary line
    if zone_obs:
        print(f"    Observed (all events): " +
              ", ".join(f"Cat-{c}: [{min(v):.1f}–{max(v):.1f}%]"
                        for c, v in sorted(zone_obs.items())))


# ── JRC source handler ────────────────────────────────────────────────────────

def run_jrc(zones: list, obs: dict, source_id: str,
            dry_run: bool, conn) -> dict:
    """
    Process all zones for JRC source.
    Returns {zone_id: {'curve': {...}, 'monotonic': bool, 'plausible': bool, 'error': str|None}}.
    """
    results = {}
    product = SOURCES_META["jrc"]["product_name"]

    for i, (zone_id, geom_json) in enumerate(zones):
        print(f"  [{i+1}/{len(zones)}] {zone_id} ... ", end="", flush=True)
        try:
            geom_ee = ee.Geometry(json.loads(geom_json))
            curve   = compute_jrc_curve(geom_ee)
            mono    = is_monotonic(curve)
            plaus   = has_plausible_range(curve)
            print(f"OK  mono={'Y' if mono else 'N'} plaus={'Y' if plaus else 'N'}")
            print_curve_vs_observed(zone_id, "jrc", curve, obs)

            rows_to_write = [
                {
                    "zone_id":          zone_id,
                    "source_product":   product,
                    "return_period_yr": rp,
                    "y_pct":            y_pct,
                    "native_scale_m":   JRC_SCALE_M,
                    "source_metadata_id": source_id,
                }
                for rp, y_pct in curve.items()
            ]
            if not dry_run:
                write_rows(conn, rows_to_write)

            results[zone_id] = {
                "curve":     curve,
                "monotonic": mono,
                "plausible": plaus,
                "error":     None,
            }

        except Exception as exc:
            print(f"ERROR: {exc}")
            results[zone_id] = {
                "curve":     None,
                "monotonic": False,
                "plausible": False,
                "error":     str(exc),
            }

    return results


# ── Gate evaluation ───────────────────────────────────────────────────────────

def evaluate_gate(source_results: dict, source_name: str) -> bool:
    """
    PASS if ≥1 zone has a monotonic, plausible curve.
    Print per-source GATE line.
    Returns True if PASS.
    """
    n_ok    = sum(1 for r in source_results.values()
                  if r["monotonic"] and r["plausible"])
    n_total = len(source_results)
    n_err   = sum(1 for r in source_results.values() if r["error"])

    verdict = "PASS" if n_ok >= 1 else "FAIL"
    print(f"\nGATE: {verdict} [{source_name}]  "
          f"zones_pass={n_ok}/{n_total}  errors={n_err}")

    if verdict == "FAIL":
        print(f"  Reason: no zone returned a monotonic Y%(RP) curve "
              f"with any RP in [{GATE_Y_MIN}%, {GATE_Y_MAX}%]")
    return verdict == "PASS"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch external flood hazard Y%(RP) and sanity-gate."
    )
    parser.add_argument(
        "--source", choices=["jrc", "floodhub", "all"], default="all",
        help="Source to run (default: all)",
    )
    parser.add_argument(
        "--zone-pattern", default=".*_initial",
        help="Regex filter on zone_id (default: .*_initial = all initial zones)",
    )
    parser.add_argument(
        "--zone-id", default=None,
        help="Single zone_id to process (overrides --zone-pattern)",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Process first zone × jrc only; still writes to DB",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print; skip all DB writes",
    )
    args = parser.parse_args()

    if args.test:
        # --test implies jrc source only
        args.source = "jrc"

    print(f"=== fetch_external_y.py ===")
    print(f"source      : {args.source}")
    print(f"zone_pattern: {args.zone_pattern if not args.zone_id else '(overridden by --zone-id)'}"
          f"  [default=.*_initial targets the 10 km study zones]")
    print(f"zone_id     : {args.zone_id or '(all matching pattern)'}")
    print(f"test        : {args.test}")
    print(f"dry_run     : {args.dry_run}")
    print()

    # ── Init GEE ────────────────────────────────────────────────────────────
    try:
        init_gee()
    except Exception as exc:
        print(f"ERROR: GEE initialisation failed: {exc}")
        sys.exit(1)

    # ── DB setup ────────────────────────────────────────────────────────────
    try:
        with psycopg2.connect(CONN_STRING) as conn:
            if not args.dry_run:
                ensure_table(conn)
                print("DB: external_flood_hazard table ready")

            zones = load_zones(conn, args.zone_pattern, args.zone_id)
            if not zones:
                print("ERROR: no study zones matched the filter.")
                sys.exit(1)

            if args.test:
                zones = zones[:1]

            print(f"Zones to process: {len(zones)}")
            for z, _ in zones:
                print(f"  {z}")
            print()

            obs = load_observed(conn)
            print(f"Observed flood fractions loaded for "
                  f"{len(obs)} zone(s) from zone_flood_analysis.\n")

    except psycopg2.Error as exc:
        print(f"ERROR: database error during setup: {exc}")
        sys.exit(1)

    any_pass  = False
    all_errors = False  # set False if any source ran without pure-error result

    # ── JRC ─────────────────────────────────────────────────────────────────
    if args.source in ("jrc", "all"):
        print("── Source: JRC/CEMS_GLOFAS/FloodHazard/v2_1 ──────────────────")
        try:
            with psycopg2.connect(CONN_STRING) as conn:
                source_id = (
                    upsert_source(conn, SOURCES_META["jrc"])
                    if not args.dry_run
                    else "00000000-0000-0000-0000-000000000000"
                )
                jrc_results = run_jrc(zones, obs, source_id, args.dry_run, conn)

            passed = evaluate_gate(jrc_results, "jrc")
            if passed:
                any_pass = True
        except psycopg2.Error as exc:
            print(f"ERROR: database error during JRC run: {exc}")
            sys.exit(1)
        except Exception as exc:
            print(f"ERROR: unexpected error during JRC run: {exc}")
            sys.exit(1)
        print()

    # ── Flood Hub ────────────────────────────────────────────────────────────
    if args.source in ("floodhub", "all"):
        print("── Source: Google Flood Hub / GRRR ────────────────────────────")
        print("  SKIP: No public GEE inundation/return-period product found for")
        print("        this region (South Asia / Pakistan). Flood Hub exposes an")
        print("        API but no GEE ImageCollection with RP-depth bands.")
        print("        Recorded as limitation; not blocking the gate.")
        print("GATE: SKIP [floodhub]  (no GEE asset available for region)")
        print()

    # ── Overall verdict ──────────────────────────────────────────────────────
    if args.source == "floodhub":
        # Only floodhub requested → can't gate on it; exit cleanly
        print("GATE: SKIP [overall]  only floodhub requested; no data available")
        print("Summary: 0 zones written. No data produced.")
        sys.exit(0)

    verdict = "PASS" if any_pass else "FAIL"
    print(f"═══════════════════════════════════════════════════════")
    print(f"GATE: {verdict} [overall]")
    if verdict == "PASS":
        print("  ≥1 source returned a monotonic, plausible Y%(RP) curve.")
        print("  Architecture is viable. Proceed to Step 2 (field collection).")
    else:
        print("  No source returned a credible curve.")
        print("  STOP: do not start Step 2 until this is resolved.")
    print(f"═══════════════════════════════════════════════════════")

    n_written = (
        sum(len(r["curve"]) for r in jrc_results.values()
            if r["curve"] is not None)
        if args.source in ("jrc", "all") else 0
    )
    dry_tag = " (dry-run; no rows written)" if args.dry_run else ""
    print(f"Summary: {len(zones)} zone(s), "
          f"{n_written} external_flood_hazard rows{'  DRY-RUN' if args.dry_run else ' written'}.{dry_tag}")

    sys.exit(0)


if __name__ == "__main__":
    main()
