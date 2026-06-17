# Task: Field-level dataset collection — `agents/collect_field_dataset.py`

**Phase 2, Step 2** of `instructions/phase_2_plan.md`. **Gated behind Step 1 `GATE: PASS`.**

## Purpose
Build the field-level modelling dataset: one row per (FoTW field × flood event) with the
ground-truth flood-% **label**, field **static terrain metrics**, and **`ndvi_premonsoon`**.
The unit switches from pixel to **FoTW field** (`phase_2_plan.md` §3 confirms fields are
viable — the scary 1.4% was sample-pixel overlap, not zone coverage).

## Label (per field × event)
`flood_pct` = fraction of the field's pixels labelled flooded for that event, from the
existing `pixel_flooded` table aggregated to FoTW field via spatial join.

**Pixel-support guard (critical, §3):** each field's label must be well-supported. Record
`n_pixels` per field; store all rows but flag/exclude fields below a support threshold
(default `MIN_PIXELS=5`, configurable) from the modelling export. Print the support
distribution so the threshold is an evidenced decision, not an assumption.

## Features (per field, static + seasonal)
- **Static terrain** aggregated per field from `pixels_static` (mean/min/max/std as
  appropriate): `elevation, slope, hand, twi, flow_acc, dist_to_channel_m, dist_to_river_m,
  strahler_order, curvature, landcover (mode), soil_hsg (mode)`.
- **Seasonal:** `ndvi_premonsoon` (per field-year), `soil_moisture`, `precip_premonsoon`.

**Zero-variance gate (§3, §5, §8.4 — mandatory before any drop):** compute and print
variance + summary stats for **every** feature. Drop a feature **only** if it is actually
constant. Do **not** drop `soil_moisture`/`precip_premonsoon` on assumption — Phase 1 never
proved them constant and they had non-zero importance (`reports/phase_one_analysis.md`).
Emit a `VARIANCE REPORT` block; drop decisions must cite it.

## New table
```sql
CREATE TABLE IF NOT EXISTS field_dataset (
    field_id        text    NOT NULL,         -- FoTW field id
    flood_event_id  uuid    NOT NULL REFERENCES flood_events(event_id),
    zone_id         text    NOT NULL REFERENCES study_zones(zone_id),
    flood_pct       double precision NOT NULL,
    n_pixels        integer NOT NULL,
    features        jsonb   NOT NULL,         -- {feature_name: value}
    source_metadata_id uuid REFERENCES data_sources(source_id),
    PRIMARY KEY (field_id, flood_event_id)
)
```
`ON CONFLICT (field_id, flood_event_id) DO UPDATE`. `zone_id` enables the location-disjoint
split (the harness groups on it).

## Conventions (CLAUDE.md)
`SUPABASE_CONN_STRING` via `.env`; `with psycopg2.connect(...)`; `%(name)s` params; stdout
progress; exit 0/1. `--test` (single zone), `--zone-pattern`, `--dry-run`, `--min-pixels N`.
Wrap per-zone in try/except; final summary: fields written / below-support / dropped features.

## Run order
`ingest_s1_flood_pixels.py` + `select_study_pixels.py` (pixel labels + terrain exist) →
**`collect_field_dataset.py`**. FoTW fields downloaded as in Phase 1 notebook (cell 23).

## Out of scope
Embeddings (separate brief, gated), modelling, calibration.
