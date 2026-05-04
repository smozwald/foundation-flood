# Task: DFO flood data extractor — single-station pipeline

Two scripts are required. Build and validate against station **000257** before
batching. Do not batch until both scripts pass `--test` cleanly.

---

## Script 1 — `agents/dfo_discharge_ingest.py`

### Purpose
Fetch discharge data and flood th
reshold values from the DFO Flood Observatory
for one station, then populate:
- `data_sources`
- `discharge_stations`
- `flood_thresholds` (5 return-period categories)
- `discharge_ts` (daily values from 2015-01-01)
- `flood_events` (derived from continuous threshold exceedances)

`zone_id` and `river_id` on `discharge_stations` are left NULL — those are
filled by Script 2 after GEE delineation.

### Data sources

**Station list:**
`https://floodobservatory.colorado.edu/wiki/DischargeFromSpace_Tab`

Columns to verify on fetch: station ID, RiverName, country,
coordinates

Note: the master page doesn't contain threshold values It does **not** contain the five return-period
categories. Those must be fetched from the individual station page. Each of these is linked through the station ID

**Per-station page:**
Each station has its own page on the DFO site. Discover the URL pattern by
inspecting the station list CSV (look for a URL or ID column) or from the DFO
site navigation. Do not hardcode — verify the actual URL structure for station
000257 first.

The station page contains:
1. A downloadable CSV of daily discharge values.
2. A flood-category visualisation (table or chart) showing discharge threshold
   values for five return-period bands: 1.5–2 yr, 2–5 yr, 5–10 yr, 10–20 yr,
   20+ yr. (This is the second of two figures)

**Threshold extraction strategy:**
- First attempt: parse threshold values from the HTML page (look for a table,
  data attributes, or embedded JSON). Preferred — log `derived_from = 'html_parse'`.
- Fallback: if values are only in a chart image, fetch the image and extract
  values using an OCR / chart-reading approach. Log `derived_from = 'image_ocr'`.
- Do not attempt to calculate return periods from the discharge time series
  itself — the record from 2015 is too short (~10 years) to fit a reliable
  frequency curve.

It is likely the first attempt will fail. Initially, explore the data of the page and ensure you can download the csv and parse the image as it will be the same method on each page that you will create a script for.
### Target tables and fill logic

#### TABLE `data_sources`
Upsert one row for DFO before anything else:
- `product_name = 'DFO Flood Observatory'`
- `version_tag = 'scraped:<ISO date>'`

Use this `source_id` as the FK for all rows written by this script.

#### TABLE `discharge_stations`
One row per station. Columns to populate:
- `dfo_station_id` — DFO numeric ID string (e.g. `'000257'`)
- `station_name` — river name from the station list
- `geom` — `ST_SetSRID(ST_MakePoint(lon, lat), 4326)`
- `river_id` — **leave NULL** (Script 2 fills after GEE)
- `zone_id` — **leave NULL** (Script 2 fills after GEE)
- `source_id` — FK to the DFO `data_sources` row
- `record_start`, `record_end` — min/max obs_date from the discharge CSV

`ON CONFLICT (dfo_station_id) DO UPDATE` for idempotency.

#### `flood_thresholds`
Five rows per station, derived (unless you find html values, dont look too hard as i think they dont exist) from image:

| category | return_period_label |
|----------|---------------------|
| 0       |  Normal discharge |
| 1        | 2-5 yr            |
| 2        | 5-10 yr              |
| 3        | 10-25 yr             |
| 4        | 25-50 yr            |
| 5        | 50+ yr              |

- `discharge_threshold_m3s` — parsed from the station page
- `derived_from` — `'html_parse'` or `'image_ocr'`
- `valid_from` / `valid_to` — NULL unless DFO provides explicit dates

`ON CONFLICT (station_id, category) DO UPDATE`.

#### `discharge_ts`
- Fetch the full daily discharge CSV from the station page.
- Filter to `obs_date >= 2015-01-01`.
- Fill no value dates or dates past end of file calendar with NULL up to end 31-12-2025.
- `discharge_m3s` — raw value from CSV
- `discharge_anomaly_pct` — leave NULL (compute in a later pass)
- `threshold_exceeded_category` — highest category whose threshold is exceeded
  by that day's discharge; 0 if below all thresholds
- `qc_flag` — `'raw'`
- `source_id` — FK to DFO data source

Primary key is composite `(station_id, obs_date)`.
`ON CONFLICT (station_id, obs_date) DO NOTHING` for idempotency.

#### `flood_events`
Derive events from `discharge_ts` after the time series is inserted:
- An event begins when `threshold_exceeded_category` transitions from NULL to
  any value, and ends when it returns to NULL.
- Consecutive above-threshold days form one event even if the category changes.
- `duration_days` — compute as `flood_end - flood_start + 1` on insert (not a
  generated column — compute in Python and pass as a value).
- `river_id` — **leave NULL** (Script 2 back-fills)
- `detection_method` — `'threshold_exceedance'`

Generate `event_id` with `uuid_generate_v4()` (or Python `uuid.uuid4()`).
`ON CONFLICT (event_id) DO NOTHING`.

### Flags
- `--test` — process station `000257` only; print all parsed values; do not insert
- `--dry-run` — process all stations, print, do not insert
- `--station ID` — run for a single station ID
- default — full batch with checkpointing (skip stations where a row already
  exists in `discharge_stations` with `record_end IS NOT NULL`)

### Error handling
- Wrap each station in try/except — one failure must not stop the batch.
- Print `[X/TOTAL] station_id — ok | error: <msg>` for every station.
- Print summary at end: processed / skipped / errors.
- Exit 0 on success, 1 if any errors occurred.

---

## Script 2 — `agents/gee_study_zones.py`

### Purpose
Use Google Earth Engine to delineate the river centreline, then generate three
non-overlapping 3 × 3 km study zones per station. Populate `rivers` and
`study_zones`, then back-fill the nullable FKs on `discharge_stations` and
`flood_events`.

### GEE approach

**River centreline:**
- Seed from the station's lat/lon in `discharge_stations`.
- Use the WWF HydroSHEDS drainage network (or equivalent GEE dataset) to snap
  to and extract the river centreline. Clip to a reasonable bounding box around
  the station.
- Store as `geometry(LineString, 4326)` in `rivers.geom`.

**Study zone generation — three 3 × 3 km squares:**

| position | zone_id pattern    | placement rule                                         |
|----------|--------------------|--------------------------------------------------------|
| 1        | `{id}_3km_1`       | centred on station point                               |
| 2        | `{id}_3km_2`       | immediately downstream of zone 1, non-overlapping      |
| 3        | `{id}_3km_3`       | immediately downstream of zone 2, non-overlapping      |

- All zones are axis-aligned squares constructed in EPSG:3857 (metric) then
  reprojected to EPSG:4326 for storage.
- "Downstream" is determined by following the river centreline direction
  extracted from HydroSHEDS. Step exactly 3 km along the centreline for each
  successive zone.
- Populate `distance_m_along_river` for zones 2 and 3 (3000 m and 6000 m from
  the station respectively; zone 1 is 0 m).

**Spatial join for `river_id`:**
After inserting the river row, link `discharge_stations` to `rivers` via a
spatial join: find the nearest river geometry to each station point and set
`discharge_stations.river_id`. Do not rely on name matching.

### Target tables

#### `rivers`
- `river_name` — from `discharge_stations.station_name`
- `geom` — GEE-derived river centreline (LineString, 4326)
- `river_source_metadata_id` — NULL for now

After inserting, run:
```sql
UPDATE discharge_stations
SET river_id = %(river_id)s
WHERE station_id = %(station_id)s
```
and
```sql
UPDATE flood_events
SET river_id = %(river_id)s
WHERE station_id = %(station_id)s
```

#### `study_zones`
Columns to populate per zone:
- `zone_id` — `{dfo_station_id}_3km_{position}`
- `river_id`, `station_id` — FKs (station_id set only on position = 1)
- `geom` — 3 × 3 km polygon (4326)
- `centroid` — `ST_Centroid(geom)`
- `zone_set` — `'3km'`
- `position` — 1, 2, or 3
- `zone_size_m` — 3000
- `generation_method` — `'hydrosheds_centreline_step'` -- we want to make this unique and linked to this code in the database so that we can keep potential datasets seperate later.
- `distance_m_along_river` — 0, 3000, 6000 for positions 1, 2, 3 -- This is in relation to the nearest point on river to station. They may be further as a meandering of a river will mean 3000m might be within the same box as position 0. We can do -numbers for upstream locations later.

After inserting, back-fill `discharge_stations.zone_id` to the position-1 zone
for each station.

### Flags
- `--test` — station 000257 only; print GeoJSON of each zone; do not insert
- `--station ID` — run for a single station
- default — full batch over all stations in `discharge_stations`

### Error handling
Same pattern as Script 1: per-station try/except, progress printing, summary,
exit codes.

---

## Dependency / run order

```
db_setup.py   →   Script 1 (dfo_discharge_ingest.py)   →   Script 2 (gee_study_zones.py)
```

Script 2 back-fills are UPDATE statements and are safe to re-run.

---

## Open questions to resolve before coding

1. **Threshold source format** — for station 000257, confirm whether the
   five-category threshold values appear as text in the HTML or only in a
   rendered chart image. This determines the extraction path. I am 99% sure its only in the imag.e
2. **DFO per-station URL pattern** — inspect the station list CSV for a URL
   field or derive the pattern from the DFO site for station 000257 before
   generalising.
3. **HydroSHEDS coverage** — It is possible that the surrounds are less than the 3x3km bounding box or downstream we don't have anything. We should handle errors such as these (we can basically have less big bounding box or have we can exclude where no data is possible)
