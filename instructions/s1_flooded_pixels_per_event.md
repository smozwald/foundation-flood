# Task: S1 per-pixel flood ingestion — `agents/ingest_s1_flood_pixels.py`

## Purpose

For every SUCCESS row in `zone_flood_analysis`, sample per-pixel Sentinel-1
backscatter values at all pixels in `pixels_static` that belong to that zone.
Write raw VV/VH values to `ts_sentinel1` and the derived flood label to a new
table `pixel_flooded`.

This is the pixel-level complement to `calculate_total_flood.py`, which only
stored zone-level aggregate counts. The GEE logic (orbit selection, dry
composite, wet scene selection) must be taken directly from that script — do
not re-derive it.

---

## On binary vs continuous flood label

Use a **boolean `is_flooded`** column in `pixel_flooded` — not a fraction.

Rationale: the Otsu threshold is applied to the dB change image
(`VV_wet − VV_dry`). Every pixel either crosses the threshold or it does not;
there is no sub-pixel mixture at 10 m resolution that produces a meaningful
fraction. A fraction would imply spatial aggregation that hasn't happened.

The continuous flood signal is preserved in `ts_sentinel1.vv` (raw dB) and in
`pixel_flooded.vv_change_db` (wet − dry). Downstream models can treat
`is_flooded` as the hard label and `vv_change_db` as the soft/continuous
feature — that combination is strictly more useful than a fraction.

---

## New table: `pixel_flooded`

```sql
CREATE TABLE IF NOT EXISTS pixel_flooded (
    pixel_id        text    NOT NULL REFERENCES pixels_static(pixel_id),
    flood_event_id  uuid    NOT NULL REFERENCES flood_events(event_id),
    analysis_id     uuid    REFERENCES zone_flood_analysis(analysis_id),
    is_flooded      boolean NOT NULL,
    vv_change_db    double precision,
    wet_obs_date    date,
    source_metadata_id uuid REFERENCES data_sources(source_id),
    PRIMARY KEY (pixel_id, flood_event_id)
)
```

Create this in `ensure_tables()` with `CREATE TABLE IF NOT EXISTS`.
Upsert one row per (pixel_id, flood_event_id).

`ON CONFLICT (pixel_id, flood_event_id) DO UPDATE SET is_flooded = EXCLUDED.is_flooded, vv_change_db = EXCLUDED.vv_change_db`

---

## `ts_sentinel1` population

Schema (already exists):
```
pixel_id text, obs_date date, vv float, vh float, incidence_angle float, metadata jsonb
PK: (pixel_id, obs_date)
```

Write **two observation types** per (pixel, analysis):

| observation_type  | obs_date               | vv / vh                       |
|-------------------|------------------------|-------------------------------|
| `'wet_event'`     | `wet_scene_date`       | pixel-level S1 scene values   |
| `'dry_composite'` | `dry_end` from analysis| pixel-level dry composite vals|

Store in `metadata` JSONB:
```json
{
  "flood_event_id": "...",
  "analysis_id": "...",
  "observation_type": "wet_event" | "dry_composite",
  "orbit_direction": "DESCENDING" | "ASCENDING"
}
```

`ON CONFLICT (pixel_id, obs_date) DO NOTHING` — do not overwrite existing rows.

---

## GEE approach

### Reuse from `calculate_total_flood.py`

Import or copy these functions verbatim — do not diverge from the existing
pipeline logic or the results will be inconsistent with `zone_flood_analysis`:

- `to_linear(img)` — linear ↔ dB conversion
- `get_best_orbit(zone_geom)` — orbit/pass direction selection
- `build_s1(zone_geom, pass_dir, orbit)` — filtered S1 collection
- `build_dry_composite(s1_col, dry_start, dry_end)` — median dry image
- `build_wet_scene(s1_col, peak_date)` — closest scene to peak discharge

The `dry_start`, `dry_end`, `wet_scene_date`, `peak_discharge_date`, and
`otsu_thresh_db` for each analysis row must be read from `zone_flood_analysis`,
not recomputed.

### Pixel sampling

Use `ee.Image.sampleRegions()` at 10 m scale with `tileScale=4`.

Build a FeatureCollection from the pixel centroids (lon/lat from `pixels_static.geom`),
attaching `pixel_id` as a property so results can be joined back.

Batch in groups of **500 pixels** (same as `select_study_pixels.py`
`TERRAIN_BATCH`) to stay within GEE memory limits.

For each batch:
1. Sample the **wet scene** (VV, VH bands; incidence_angle from `angle` band if
   available, else NULL). Compute VV in dB: `10 * log10(VV_linear)`. The raw S1
   GRD image is in linear power; `build_s1` already applies `to_linear` on
   retrieval — verify before sampling.
2. Sample the **dry composite** the same way.
3. Compute `vv_change_db = vv_wet_db − vv_dry_db`.
4. `is_flooded = (vv_change_db < otsu_thresh_db)` where `otsu_thresh_db` comes
   from `zone_flood_analysis`.

---

## Data source registration

Upsert one row in `data_sources` before writing:

```python
{
    'product_name':    'Sentinel-1 GRD — SAR flood pixels',
    'version_tag':     f'ingest:{date.today().isoformat()}',
    'resolution_m':    10.0,
    'methodology_desc': (
        'Sentinel-1 GRD IW VV/VH sampled per pixel via GEE sampleRegions(). '
        'Wet scene: closest S1 acquisition to peak discharge date within '
        '±12 days. Dry composite: median S1 over Feb 15–May 15 preceding the '
        'flood year. Flood label: is_flooded = (VV_wet_db − VV_dry_db) < otsu_thresh_db '
        'where otsu_thresh_db is taken from zone_flood_analysis.'
    ),
}
```

Use `ON CONFLICT (product_name) DO UPDATE` and return `source_id` for use as
FK in `pixel_flooded.source_metadata_id` and `ts_sentinel1.metadata`.

---

## Processing logic

### Pair selection

Load all SUCCESS rows from `zone_flood_analysis`, filtered by `--zone-pattern`
(POSIX regex on `zone_id`). Default: `'.*'` (all zones).

Skip pairs where ALL pixels for that zone already have rows in `pixel_flooded`
for that `flood_event_id` — i.e. the pair is fully ingested.
Partially-ingested pairs should be re-processed (idempotent upsert handles duplicates).

### Per-analysis loop

For each qualifying (zone_id, flood_event_id) row:

```
1. Load zone geom from study_zones.
2. Load all pixels (pixel_id, lon, lat) from pixels_static for that zone_id.
3. Initialise S1 collection: get_best_orbit → build_s1.
4. Build dry_composite using (dry_start, dry_end) from zone_flood_analysis.
5. Build wet scene using (peak_discharge_date) from zone_flood_analysis;
   fall back to flood_start if peak_discharge_date IS NULL.
6. Batch-sample wet + dry at pixel centroids (500 per batch).
7. Write ts_sentinel1 rows: wet observation + dry observation.
8. Write pixel_flooded rows: is_flooded + vv_change_db.
9. Print progress: [zone_id / event_id] N pixels written.
```

---

## Flags

| flag | behaviour |
|------|-----------|
| `--zone-pattern TEXT` | POSIX regex matched against `zone_id` (default `'.*'`) |
| `--zone-id TEXT` | single zone override |
| `--reprocess` | ignore existing `pixel_flooded` rows; re-sample and overwrite |
| `--test` | first qualifying analysis pair only; print sample output; still writes |
| `--dry-run` | compute and print counts; do not insert |

---

## Error handling

Wrap each (zone, event) pair in `try/except`. One failure must not stop the
batch. Print `[zone_id :: event_id] ok | error: <msg>` per pair. Print summary
at end: processed / skipped / errors. Exit 0 on success, 1 if any errors.

---

## Run order

```
calculate_total_flood.py   (must have SUCCESS rows in zone_flood_analysis)
select_study_pixels.py     (must have pixels in pixels_static with geom)
        ↓
ingest_s1_flood_pixels.py
```

---

## Do not change

- The Otsu threshold values in `zone_flood_analysis` — read them, do not
  recompute.
- The orbit/pass-direction logic from `calculate_total_flood.py`.
- The `pixels_static` rows — this script is read-only on that table.
- The `ts_sentinel1` primary key; use `ON CONFLICT DO NOTHING` to stay safe.
