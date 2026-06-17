#!/usr/bin/env python3
"""
validate_labels_ems.py — Independent label validation against Copernicus EMS.

Compares pixel_flooded.is_flooded (Otsu-derived SAR labels) against a
Copernicus EMS Rapid Mapping flood-extent shapefile for a matching event,
computing IoU, precision, recall, and % agreement.

Usage:
    python agents/validate_labels_ems.py [options]

Options:
    --zone-id TEXT      Study zone to validate (default: 000099_initial)
    --ems-shp PATH      Path to EMS "Observed Event" shapefile or GeoPackage.
                        If omitted, the script searches temp_images/ for a
                        file matching emsr*.shp or emsr*.gpkg, then exits
                        with a clear gap message if none is found.
    --ems-id TEXT       EMS activation ID used in provenance (e.g. EMSR838).
                        Inferred from --ems-shp filename if not provided.
    --event-year INT    Restrict to flood events in this year (default: 2025,
                        matching the Aug 2025 Cat-5 event).
    --dry-run           Compute and print metrics; do not write report.
    --test              Process only the first qualifying event.
    --out PATH          Output report path
                        (default: reports/phase_2_label_validation.md).
"""

import argparse
import os
import sys
from datetime import date

import psycopg2
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Environment                                                                  #
# --------------------------------------------------------------------------- #

load_dotenv()

CONN_STRING = os.environ.get("SUPABASE_CONN_STRING")
if not CONN_STRING:
    print("ERROR: SUPABASE_CONN_STRING not set.")
    sys.exit(1)

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT  = os.path.dirname(SCRIPT_DIR)
TEMP_IMAGES   = os.path.join(PROJECT_ROOT, "temp_images")
REPORTS_DIR   = os.path.join(PROJECT_ROOT, "reports")
DEFAULT_REPORT = os.path.join(REPORTS_DIR, "phase_2_label_validation.md")

# Known EMS activations for Pakistan floods referenced in Phase 1 notebook
# (03_analysis.py CELL 10).  These are used for provenance notes only; the
# actual data must be supplied via --ems-shp.
KNOWN_ACTIVATIONS = {
    "EMSR838": {
        "event": "Pakistan floods Aug 2025 (Chenab / Indus basin)",
        "url": "https://emergency.copernicus.eu/mapping/list-of-components/EMSR838",
        "year": 2025,
    },
    "EMSR629": {
        "event": "Pakistan floods 2022 (Chenab / Indus basin)",
        "url": "https://emergency.copernicus.eu/mapping/list-of-components/EMSR629",
        "year": 2022,
    },
}

# --------------------------------------------------------------------------- #
# DB helpers                                                                   #
# --------------------------------------------------------------------------- #

def load_pixel_labels(conn, zone_id, event_year=None):
    """Return DataFrame of pixel_id, lat, lon, is_flooded, flood_event_id, flood_start."""
    year_clause = ""
    params = {"zone_id": zone_id}
    if event_year:
        year_clause = "AND EXTRACT(YEAR FROM fe.flood_start) = %(year)s"
        params["year"] = event_year

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                ps.pixel_id,
                ST_Y(ps.geom::geometry) AS lat,
                ST_X(ps.geom::geometry) AS lon,
                pf.is_flooded,
                pf.flood_event_id::text,
                fe.flood_start,
                fe.max_category
            FROM pixel_flooded pf
            JOIN pixels_static ps ON ps.pixel_id = pf.pixel_id
            JOIN flood_events  fe ON fe.event_id = pf.flood_event_id
            WHERE ps.zone_id = %(zone_id)s
              {year_clause}
            ORDER BY fe.flood_start, ps.pixel_id
        """, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()

    if not rows:
        return None, cols
    try:
        import pandas as pd
        df = pd.DataFrame(rows, columns=cols)
        df["flood_start"] = pd.to_datetime(df["flood_start"])
        return df, cols
    except ImportError:
        return rows, cols


def load_zone_bbox(conn, zone_id):
    """Return (lon_min, lat_min, lon_max, lat_max) for the zone."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                ST_XMin(geom::geometry),
                ST_YMin(geom::geometry),
                ST_XMax(geom::geometry),
                ST_YMax(geom::geometry)
            FROM study_zones
            WHERE zone_id = %(zone_id)s
        """, {"zone_id": zone_id})
        row = cur.fetchone()
    return row  # (xmin, ymin, xmax, ymax)


# --------------------------------------------------------------------------- #
# EMS shapefile helpers                                                        #
# --------------------------------------------------------------------------- #

def find_ems_file(directory):
    """Scan directory for files matching emsr*.shp, emsr*.gpkg (case-insensitive)."""
    if not os.path.isdir(directory):
        return None
    for fname in sorted(os.listdir(directory)):
        lower = fname.lower()
        if lower.startswith("emsr") and (lower.endswith(".shp") or lower.endswith(".gpkg")):
            return os.path.join(directory, fname)
    return None


def infer_ems_id(shp_path):
    """Try to extract EMSR\\d+ from the filename."""
    import re
    m = re.search(r"(EMSR\d+)", os.path.basename(shp_path), re.IGNORECASE)
    return m.group(1).upper() if m else "UNKNOWN"


def load_ems_flood_polygons(shp_path):
    """
    Load EMS shapefile/GeoPackage and return the flood-extent GeoDataFrame.

    Tries standard EMS field filters (obj_type, symbology, TYPE) for flood
    polygons; falls back to all polygons with a warning.
    """
    try:
        import geopandas as gpd
    except ImportError:
        print("ERROR: geopandas is required. Install: pip install geopandas")
        sys.exit(1)

    gdf = gpd.read_file(shp_path).to_crs("EPSG:4326")
    print(f"  Loaded {len(gdf)} features from {os.path.basename(shp_path)}")

    filtered = None
    for col in ("obj_type", "symbology", "TYPE", "type", "class", "Class"):
        if col in gdf.columns:
            mask = gdf[col].astype(str).str.lower().str.contains(
                "flood|water|inundat", na=False
            )
            if mask.sum() > 0:
                filtered = gdf[mask].copy()
                print(f"  Filtered to {len(filtered)} flood polygons "
                      f"(column '{col}' contains flood/water/inundat)")
                break

    if filtered is None or len(filtered) == 0:
        print("  WARNING: no flood-type filter matched — using ALL polygons")
        filtered = gdf.copy()

    return filtered


# --------------------------------------------------------------------------- #
# Metrics                                                                      #
# --------------------------------------------------------------------------- #

def compute_metrics(our_labels, ems_labels):
    """
    Both inputs are boolean arrays of identical length.
    Returns dict with iou, precision, recall, pct_agreement.
    """
    a = our_labels.astype(bool)
    b = ems_labels.astype(bool)
    tp = int((a & b).sum())
    fp = int((a & ~b).sum())
    fn = int((~a & b).sum())
    tn = int((~a & ~b).sum())
    union  = tp + fp + fn
    iou    = tp / union if union > 0 else float("nan")
    prec   = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
    rec    = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
    agree  = (tp + tn) / len(a) if len(a) > 0 else float("nan")
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "iou": iou, "precision": prec, "recall": rec,
        "pct_agreement": agree * 100,
        "our_flooded_pct":  100 * a.sum()   / len(a),
        "ems_flooded_pct":  100 * b.sum()   / len(b),
        "n_pixels": len(a),
    }


def ems_point_in_polygon(pix_df, ems_gdf):
    """
    Vectorised point-in-polygon: returns boolean Series aligned to pix_df index.
    Uses a spatial index (STRtree) if shapely >= 1.8.
    """
    try:
        from shapely.geometry import Point
        import geopandas as gpd
    except ImportError:
        print("ERROR: shapely + geopandas required.")
        sys.exit(1)

    union = ems_gdf.geometry.union_all()
    pts = [Point(lon, lat) for lon, lat in zip(pix_df["lon"], pix_df["lat"])]
    return [union.contains(p) for p in pts]


# --------------------------------------------------------------------------- #
# Report writer                                                                #
# --------------------------------------------------------------------------- #

def write_report(out_path, zone_id, event_info, ems_id, ems_source_url,
                 ems_shp_path, metrics, gap_reason=None):
    """Write reports/phase_2_label_validation.md."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    today = date.today().isoformat()

    lines = [
        "# Phase 2 — Label Validation Report",
        "",
        f"**Generated:** {today}  ",
        f"**Script:** `agents/validate_labels_ems.py`  ",
        f"**Study zone:** `{zone_id}`  ",
        "",
        "---",
        "",
        "## Purpose",
        "",
        "Our flood labels are Otsu-derived SAR (self-validated). This report "
        "quantifies agreement between those labels and an independent Copernicus EMS "
        "Rapid Mapping flood-extent product for a matching event window. It does not "
        "change the labels — it establishes how much to trust them.",
        "",
        "---",
        "",
    ]

    if gap_reason:
        lines += [
            "## Gap — No independent validation performed",
            "",
            gap_reason,
            "",
            "---",
            "",
            "## Known candidate EMS activations",
            "",
            "The following Copernicus EMS activations were identified as overlapping "
            "the study area and event windows. They have **not** been downloaded or "
            "compared — this is the gap recorded here.",
            "",
        ]
        for eid, info in KNOWN_ACTIVATIONS.items():
            lines += [
                f"### {eid}",
                "",
                f"- **Event:** {info['event']}",
                f"- **Year:** {info['year']}",
                f"- **URL:** {info['url']}",
                f"- **Status:** shapefile not available in this environment",
                "",
            ]
        lines += [
            "## Implication for label trust",
            "",
            "Without independent validation, the Otsu-derived labels remain "
            "**self-validated only**. The label circularity noted in "
            "`reports/phase_one_analysis.md` (caveat 3) is unresolved.",
            "",
            "**Recommended action:** Download the observed-event shapefile from one "
            "of the candidate activations above, place it in `temp_images/` as "
            "`emsr838_observed_event.shp` (or `.gpkg`), and re-run this script.",
            "",
            "---",
            "",
            "_No metrics were computed. This file records the gap, not fabricated results._",
        ]
    else:
        # Full metrics report
        m = metrics
        ev = event_info

        if isinstance(m["iou"], float) and m["iou"] != m["iou"]:  # NaN check
            iou_str  = "NaN (no pixels in union)"
            prec_str = "NaN"
            rec_str  = "NaN"
        else:
            iou_str  = f"{m['iou']:.3f}"
            prec_str = f"{m['precision']:.3f}"
            rec_str  = f"{m['recall']:.3f}"

        lines += [
            "## Validation event",
            "",
            f"| Field | Value |",
            f"|-------|-------|",
            f"| EMS activation | {ems_id} |",
            f"| EMS source URL | {ems_source_url} |",
            f"| EMS shapefile | `{os.path.basename(ems_shp_path)}` |",
            f"| Flood event date | {ev.get('flood_start', 'unknown')} |",
            f"| Flood event category | {ev.get('max_category', 'unknown')} |",
            f"| Pixels compared | {m['n_pixels']:,} |",
            "",
            "---",
            "",
            "## Agreement metrics",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| IoU (Intersection over Union) | {iou_str} |",
            f"| Precision (our positive → EMS positive) | {prec_str} |",
            f"| Recall (EMS positive → our positive) | {rec_str} |",
            f"| % pixel agreement (TP + TN) / N | {m['pct_agreement']:.1f}% |",
            f"| Our Otsu flooded % | {m['our_flooded_pct']:.1f}% |",
            f"| EMS flooded % | {m['ems_flooded_pct']:.1f}% |",
            "",
            "**Confusion matrix:**",
            "",
            f"| | EMS: flooded | EMS: not flooded |",
            f"|---|---|---|",
            f"| Otsu: flooded | TP = {m['tp']:,} | FP = {m['fp']:,} |",
            f"| Otsu: not flooded | FN = {m['fn']:,} | TN = {m['tn']:,} |",
            "",
            "---",
            "",
            "## Statement of label trust",
            "",
        ]

        iou_val = m["iou"]
        agree_val = m["pct_agreement"]

        if isinstance(iou_val, float) and not (iou_val != iou_val):
            if iou_val >= 0.5:
                trust = (
                    f"Agreement is **substantial** (IoU {iou_str}, "
                    f"{agree_val:.1f}% pixel agreement). The Otsu-derived SAR "
                    "labels are consistent with an independent EMS rapid-mapping "
                    "product for the same event. Label quality is adequate for the "
                    "vulnerability-ranking objective."
                )
            elif iou_val >= 0.3:
                trust = (
                    f"Agreement is **moderate** (IoU {iou_str}, "
                    f"{agree_val:.1f}% pixel agreement). The Otsu and EMS labels "
                    "capture the same flood event but differ in extent — plausibly "
                    "due to resolution, timing offsets, or EMS delineation "
                    "conventions. Labels should be treated with caution; the label "
                    "circularity is partially but not fully resolved."
                )
            else:
                trust = (
                    f"Agreement is **poor** (IoU {iou_str}, "
                    f"{agree_val:.1f}% pixel agreement). The Otsu and EMS labels "
                    "diverge substantially. Possible causes: temporal mismatch "
                    "between EMS product and SAR wet scene, EMS spatial coverage "
                    "not fully overlapping the study zone, or Otsu labelling error. "
                    "The label circularity identified in Phase 1 remains unresolved; "
                    "use Otsu labels with caution."
                )
        else:
            trust = (
                "Metrics could not be computed (no pixel union). "
                "Check that the EMS shapefile actually overlaps the study zone."
            )

        lines += [
            trust,
            "",
            "---",
            "",
            "## Caveats",
            "",
            "1. **Single event.** One event comparison cannot generalise to all 64 events.",
            "2. **Resolution mismatch.** EMS products are typically delineated at 10–20 m "
            "   from Sentinel-1; our Otsu labels use the same sensor but a different "
            "   methodology (zone-level threshold, not pixel-level). Minor geometric "
            "   offsets are expected.",
            "3. **EMS timing.** EMS rapid-mapping scenes may use a different acquisition "
            "   date than the wet scene in `zone_flood_analysis`. Date alignment was not "
            "   enforced beyond matching the event year.",
            "4. **JRC GSW unavailable.** JRC GSW data ends 2021 and cannot validate the "
            "   2025 event. EMS is the appropriate independent source here.",
            "",
            "---",
            "",
            "_This report was generated by `agents/validate_labels_ems.py`._",
        ]

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nReport written: {out_path}")


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Validate Otsu flood labels against Copernicus EMS shapefile."
    )
    parser.add_argument("--zone-id",    default="000099_initial",
                        help="Study zone to validate (default: 000099_initial)")
    parser.add_argument("--ems-shp",    default=None,
                        help="Path to EMS Observed Event shapefile or GeoPackage")
    parser.add_argument("--ems-id",     default=None,
                        help="EMS activation ID (e.g. EMSR838); inferred from filename if omitted")
    parser.add_argument("--event-year", type=int, default=2025,
                        help="Flood event year to compare (default: 2025)")
    parser.add_argument("--dry-run",    action="store_true",
                        help="Compute metrics but do not write report")
    parser.add_argument("--test",       action="store_true",
                        help="Process only the first qualifying event")
    parser.add_argument("--out",        default=DEFAULT_REPORT,
                        help="Output report path")
    args = parser.parse_args()

    print(f"Zone        : {args.zone_id}")
    print(f"Event year  : {args.event_year}")
    print(f"EMS file    : {args.ems_shp or '(auto-detect from temp_images/)'}")
    print(f"Dry-run     : {args.dry_run}")
    print(f"Test        : {args.test}")
    print()

    # ── Locate EMS file ──────────────────────────────────────────────────── #

    ems_shp = args.ems_shp
    if ems_shp and not os.path.exists(ems_shp):
        print(f"ERROR: --ems-shp path not found: {ems_shp}")
        sys.exit(1)

    if not ems_shp:
        ems_shp = find_ems_file(TEMP_IMAGES)

    if not ems_shp:
        # No shapefile available — record the gap
        gap = (
            "No Copernicus EMS shapefile was found.\n\n"
            "The script searched `temp_images/` for files matching `emsr*.shp` or "
            "`emsr*.gpkg` and found none. Network access to the Copernicus EMS portal "
            "is not available in this environment (the EMS download service requires "
            "a browser session / authentication; there is no stable unauthenticated "
            "download URL).\n\n"
            "**To resolve:** manually download the 'Observed Event' product from one "
            "of the candidate activations below, extract the shapefile, and place it "
            "in `temp_images/` as e.g. `emsr838_observed_event.shp`. Then re-run "
            "this script."
        )
        print("No EMS shapefile found in temp_images/.")
        print("Recording gap in report.")
        if not args.dry_run:
            write_report(
                args.out,
                zone_id=args.zone_id,
                event_info={},
                ems_id=None,
                ems_source_url=None,
                ems_shp_path=None,
                metrics=None,
                gap_reason=gap,
            )
        else:
            print("[dry-run] Would write gap report.")
        sys.exit(0)

    ems_id = args.ems_id or infer_ems_id(ems_shp)
    ems_info = KNOWN_ACTIVATIONS.get(ems_id, {})
    ems_url = ems_info.get("url", f"https://emergency.copernicus.eu/mapping/list-of-components/{ems_id}")
    print(f"EMS ID      : {ems_id}")
    print(f"EMS URL     : {ems_url}")
    print()

    # ── Load pixel labels from DB ─────────────────────────────────────────── #

    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas is required. Install: pip install pandas")
        sys.exit(1)

    print(f"Loading pixel labels for zone '{args.zone_id}', year {args.event_year} ...")
    try:
        with psycopg2.connect(CONN_STRING) as conn:
            df, _cols = load_pixel_labels(conn, args.zone_id, args.event_year)
            bbox = load_zone_bbox(conn, args.zone_id)
    except psycopg2.Error as exc:
        print(f"DB error: {exc}")
        sys.exit(1)

    if df is None or (hasattr(df, '__len__') and len(df) == 0):
        print(f"No pixel_flooded rows found for zone '{args.zone_id}' "
              f"year {args.event_year}.")
        print("pixel_flooded may be empty — run ingest_s1_flood_pixels.py first.")
        sys.exit(1)

    print(f"  {len(df)} pixel-event rows loaded.")

    # Pick event: largest flood (highest flooded %) or first if --test
    events = df.groupby("flood_event_id").agg(
        flood_start=("flood_start", "first"),
        max_category=("max_category", "first"),
        pct_flooded=("is_flooded", lambda x: 100 * x.sum() / len(x)),
        n_pixels=("pixel_id", "count"),
    ).reset_index().sort_values("pct_flooded", ascending=False)

    print(f"\nEvents found ({len(events)}):")
    for _, ev in events.iterrows():
        print(f"  {ev['flood_start'].date()}  cat={ev['max_category']}  "
              f"{ev['pct_flooded']:.1f}% flooded  ({ev['n_pixels']} px)")

    if args.test:
        events = events.head(1)
        print(f"\nTEST mode: using first event only.")

    # Use the event with the highest flood % (strongest signal)
    chosen = events.iloc[0]
    event_id = chosen["flood_event_id"]
    event_df = df[df["flood_event_id"] == event_id].copy().reset_index(drop=True)

    print(f"\nValidating against event: {chosen['flood_start'].date()} "
          f"cat={chosen['max_category']} ({chosen['pct_flooded']:.1f}% flooded, "
          f"{len(event_df)} pixels)")

    # ── Load EMS shapefile ──────────────────────────────────────────────── #

    print(f"\nLoading EMS shapefile: {ems_shp}")
    ems_gdf = load_ems_flood_polygons(ems_shp)

    if bbox:
        xmin, ymin, xmax, ymax = bbox
        print(f"  Zone bbox: ({xmin:.4f}, {ymin:.4f}) → ({xmax:.4f}, {ymax:.4f})")
        # Clip EMS to zone bbox + 0.1 deg buffer for efficiency
        try:
            from shapely.geometry import box as shapely_box
            import geopandas as gpd
            zone_box = shapely_box(xmin - 0.1, ymin - 0.1, xmax + 0.1, ymax + 0.1)
            ems_gdf_clip = ems_gdf[ems_gdf.intersects(zone_box)].copy()
            print(f"  Clipped to zone bbox: {len(ems_gdf_clip)} of {len(ems_gdf)} polygons remain")
            if len(ems_gdf_clip) == 0:
                print("  WARNING: EMS shapefile has no polygons within zone bbox.")
                print("  The EMS activation may not cover this study zone.")
                # Still proceed — metrics will be 0 IoU
            else:
                ems_gdf = ems_gdf_clip
        except Exception as clip_err:
            print(f"  WARNING: could not clip EMS to bbox: {clip_err}")

    # ── Point-in-polygon ──────────────────────────────────────────────────── #

    print(f"\nRunning point-in-polygon for {len(event_df)} pixels ...")
    ems_labels = ems_point_in_polygon(event_df, ems_gdf)
    ems_series = pd.Series(ems_labels, index=event_df.index)

    # ── Compute metrics ──────────────────────────────────────────────────── #

    metrics = compute_metrics(event_df["is_flooded"], ems_series)

    print("\n--- Metrics ---")
    print(f"  Pixels compared   : {metrics['n_pixels']:,}")
    print(f"  Otsu flooded %    : {metrics['our_flooded_pct']:.1f}%")
    print(f"  EMS flooded %     : {metrics['ems_flooded_pct']:.1f}%")
    print(f"  IoU               : {metrics['iou']:.3f}")
    print(f"  Precision         : {metrics['precision']:.3f}")
    print(f"  Recall            : {metrics['recall']:.3f}")
    print(f"  % agreement       : {metrics['pct_agreement']:.1f}%")
    print(f"  TP={metrics['tp']:,}  FP={metrics['fp']:,}  "
          f"FN={metrics['fn']:,}  TN={metrics['tn']:,}")

    # ── Write report ─────────────────────────────────────────────────────── #

    event_info = {
        "flood_start":  chosen["flood_start"].date(),
        "max_category": chosen["max_category"],
    }

    if not args.dry_run:
        write_report(
            args.out,
            zone_id=args.zone_id,
            event_info=event_info,
            ems_id=ems_id,
            ems_source_url=ems_url,
            ems_shp_path=ems_shp,
            metrics=metrics,
            gap_reason=None,
        )
    else:
        print("\n[dry-run] Report not written.")

    sys.exit(0)


if __name__ == "__main__":
    main()
