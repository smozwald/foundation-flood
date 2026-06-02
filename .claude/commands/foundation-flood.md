# /foundation-flood — Foundation Flood pipeline assistant

You are an assistant for the foundation-flood project. When this command is
invoked, read the argument the user provided and route to the correct workflow
below. If no argument is given, print the available sub-commands.

## Available sub-commands

- `help` — overview of the project, pipeline, and all available commands
- `get-study-data` — collect all data for a new river through to populated pixels
- `flood-pixels` — calculate per-pixel flood labels for existing pixels_static rows
- `status` — report what data is already in the DB for a given river or zone pattern
- `run-step <step>` — run a single pipeline step (list steps if omitted)

If no argument is given, run `help` automatically.

---

## Sub-command: `help`

Print the following overview to the user, then ask if they want to go deeper on any section.

---

### foundation-flood — what this project does

This project builds a per-pixel agricultural flood risk dataset for rivers in
Pakistan using satellite SAR imagery. The goal is to train a model that predicts
which crop fields will flood given a rainfall scenario, using Sentinel-1 SAR
backscatter + topographic features + (later) foundation model embeddings.

**Current scope:** 10 study zones across the Chenab and Indus rivers, ~49,500
agricultural pixels, 64 confirmed flood events (2015–2025).

---

### The pipeline — 5 steps in order

```
1. dfo-ingest        Fetch discharge time series + flood events from DFO
2. define-zones      Delineate river centreline + create 3×3 km study zones via GEE
3. flood-calc        Run Otsu SAR thresholding per zone×event → zone_flood_analysis
4. select-pixels     Sample ESA WorldCover cropland pixels + terrain metrics (GEE)
5. s1-flood-pixels   Per-pixel S1 VV/VH backscatter + flood label → ts_sentinel1, pixel_flooded
```

Steps 1–4 are complete for the current 10 zones. Step 5 (`ingest_s1_flood_pixels.py`) is
implemented and ready to run.

---

### Database tables (Supabase / PostGIS)

| Table | What it holds | Status |
|-------|--------------|--------|
| `discharge_stations` | 13 Pakistan stations (DFO) | complete |
| `discharge_ts` | Daily discharge 2015–2025 | complete |
| `flood_thresholds` | 5 return-period categories per station | complete |
| `flood_events` | 318 flood event windows | complete |
| `rivers` | River centrelines (GEE HydroSHEDS) | complete |
| `study_zones` | 3×3 km zones per station | complete |
| `zone_flood_analysis` | Otsu SAR flood masks, 64 SUCCESS rows | complete |
| `pixels_static` | 49,450 cropland pixels + terrain metrics | complete |
| `study_zone_dataset` | Pixel sampling run metadata | complete |
| `data_sources` | Provenance for all GEE/DFO sources | complete |
| `ts_sentinel1` | Per-pixel S1 VV/VH time series | **empty — next step** |
| `pixel_flooded` | Per-pixel flood label per event | **empty — next step** |
| `ts_sentinel2` | Per-pixel S2 optical time series | not started |
| `ts_meteo` | Meteorological time series | not started |
| `pixel_embeddings` | Foundation model embeddings | not started |

---

### Scripts in `agents/`

| Script | Does | Run order |
|--------|------|:---------:|
| `db_setup.py` | Creates all tables | 0 |
| `dfo_discharge_ingest.py` | DFO scrape → discharge_ts, flood_events | 1 |
| `define_study_zones.py` | GEE river delineation → study_zones | 2 |
| `calculate_total_flood.py` | Otsu SAR → zone_flood_analysis | 3 |
| `select_study_pixels.py` | GEE crop pixels + terrain → pixels_static | 4 |
| `ingest_s1_flood_pixels.py` | GEE S1 per-pixel → ts_sentinel1, pixel_flooded | 5 |

All scripts accept `--test` (single record) and exit 0/1.

---

### Instruction briefs in `instructions/`

These are task briefs for implementing or extending agents:

| File | What it specifies |
|------|------------------|
| `s1_flooded_pixels_per_event.md` | `ingest_s1_flood_pixels.py` (implemented) |
| `s2_dry_season_water_mask.md` | S2 MNDWI water mask to improve Otsu baseline |
| `dfo_flood_extractor.md` | Original DFO ingest + study zone pipeline |
| `db_setup.md` | Schema design reference |

---

### What to do next

The immediate next step is running `ingest_s1_flood_pixels.py` to populate
`ts_sentinel1` and `pixel_flooded`. Recommended starting point:

```bash
python agents/ingest_s1_flood_pixels.py --zone-id 000099_initial --test
```

Zone `000099_initial` (Chenab, 6,233 pixels, 10 events) has the clearest
flood signal — a category 5 event in Aug 2025 shows 37% flooded vs 6–18% for
category 1 events.

---

### Commands you can use here

| Command | What it does |
|---------|-------------|
| `/foundation-flood help` | This overview |
| `/foundation-flood status` | Live DB row counts for any river or zone |
| `/foundation-flood flood-pixels` | Guided run of `ingest_s1_flood_pixels.py` |
| `/foundation-flood get-study-data` | Onboard a new river end-to-end |
| `/foundation-flood run-step <name>` | Get the exact command for one pipeline step |

---

## Sub-command: `get-study-data`

### Purpose
Walk the user through the full data collection pipeline for a new river, from
DFO station selection through to `pixels_static` populated with terrain metrics.

### Step 1 — Structured interview

Ask the user these questions one at a time. Do not proceed to the next until you
have a confirmed answer. Offer defaults where marked.

1. **River name**: What is the river name? (e.g. "Jhelum River")
2. **DFO station ID**: Do you know the DFO station ID (6-digit number)?
   If not, offer to search `discharge_stations` in Supabase or ask them to
   check the DFO Flood Observatory.
3. **Zone pattern**: What label should be used for the `zone_id` suffix?
   (default: `initial`) — this becomes `{station_id}_{label}` e.g. `000099_initial`.
4. **Study area size**: What study zone size in metres? (default: 3000 m)
5. **Pixel sampling**: How many subzones per zone? (default: 12, width 500 m,
   min distance 100 m from river). Accept defaults or let user customise.
6. **Date range**: Any restriction on flood events to process?
   (default: all events from 2015-01-01 in `flood_events` for this station)

Confirm the full config back to the user before proceeding.

### Step 2 — Check existing data

Before running any scripts, query Supabase to report what already exists:
- Is the station in `discharge_stations`?
- Does a river geometry exist in `rivers`?
- Do study zones exist in `study_zones` for this station?
- Are there SUCCESS rows in `zone_flood_analysis`?
- Are there pixels in `pixels_static` for this zone pattern?

Report what's present and what's missing, then ask the user whether to proceed
with only the missing steps or re-run everything.

### Step 3 — Instruct which scripts to run in order

Tell the user exactly which commands to run, in order. Use `--test` first for
each step:

```
# Step 1: discharge + flood events (skip if station already in DB)
python agents/dfo_discharge_ingest.py --station {station_id} --test
python agents/dfo_discharge_ingest.py --station {station_id}

# Step 2: river geometry + study zones (skip if zones already in DB)
python agents/define_study_zones.py --station {station_id} --test
python agents/define_study_zones.py --station {station_id}

# Step 3: S1 flood mask per event
python agents/calculate_total_flood.py --zone-set {label} --rivers "{river_name}" --test
python agents/calculate_total_flood.py --zone-set {label} --rivers "{river_name}"

# Step 4: select agricultural pixels + terrain
python agents/select_study_pixels.py --zone-pattern "{station_id}_{label}" --test
python agents/select_study_pixels.py --zone-pattern "{station_id}_{label}"

# Step 5: per-pixel S1 flood ingestion
python agents/ingest_s1_flood_pixels.py --zone-pattern "{station_id}_{label}" --test
python agents/ingest_s1_flood_pixels.py --zone-pattern "{station_id}_{label}"
```

If `ingest_s1_flood_pixels.py` does not yet exist, say so and point to
`instructions/s1_flooded_pixels_per_event.md` for the implementation brief.

### Step 4 — Monitor and unblock

After the user runs each step, they can paste the output back. If a step fails:
- Diagnose the error from the output
- Suggest a fix or alternative flag
- Do not re-run steps that already succeeded

---

## Sub-command: `flood-pixels`

### Purpose
Run `agents/ingest_s1_flood_pixels.py` to populate `ts_sentinel1` and
`pixel_flooded` for pixels already in `pixels_static`.

### Step 1 — Confirm scope

Ask the user these questions before running anything:

1. **Which pixels?** Zone pattern (POSIX regex on `zone_id`) or a single `zone_id`?
   Show what patterns are available by querying:
   ```sql
   SELECT DISTINCT zone_id, COUNT(*) AS pixels
   FROM pixels_static GROUP BY zone_id ORDER BY zone_id;
   ```
   Let them pick from that list or enter a custom pattern.

2. **Reprocess?** Are there already rows in `pixel_flooded` for these zones?
   Check with:
   ```sql
   SELECT COUNT(*) FROM pixel_flooded pf
   JOIN pixels_static ps ON ps.pixel_id = pf.pixel_id
   WHERE ps.zone_id ~ '<pattern>';
   ```
   If rows exist, ask: skip already-done events (default) or `--reprocess` to overwrite?

3. **Confirm tools**: This script uses:
   - **Google Earth Engine** (GEE) — to sample S1 backscatter at pixel level.
     Requires `earthengine authenticate --project foundation-flood` to be active.
   - **Supabase / PostGIS** — reads `zone_flood_analysis`, `pixels_static`;
     writes `ts_sentinel1`, `pixel_flooded`.
   Ask: "GEE auth is required. Are you happy to proceed with both GEE and Supabase?"
   If GEE auth might be stale, suggest: `! earthengine authenticate --project foundation-flood`

4. **Test first?** Always suggest `--test` for the first run on a new zone pattern.

### Step 2 — Show the command

After confirmed answers, print the exact command to run:

```bash
# Test run (first analysis only — recommended first)
python agents/ingest_s1_flood_pixels.py --zone-pattern "<pattern>" --test

# Full run
python agents/ingest_s1_flood_pixels.py --zone-pattern "<pattern>"

# Single zone
python agents/ingest_s1_flood_pixels.py --zone-id <zone_id>

# Re-run and overwrite existing labels
python agents/ingest_s1_flood_pixels.py --zone-pattern "<pattern>" --reprocess
```

### Step 3 — After the run

Ask the user to paste the output. Check for:
- `errors=N` in the summary — diagnose and suggest fixes if N > 0
- Low hit counts (e.g. "42 hits" from 6000 pixels) — suggests GEE sampling issue
- "no scene found" — wet scene outside +12-day window; normal for some events

---

## Sub-command: `status`

Ask the user for a river name or zone pattern, then run these checks and report:

```sql
-- Discharge stations
SELECT dfo_station_id, station_name, record_start, record_end
FROM discharge_stations WHERE station_name ILIKE '%{name}%';

-- Flood events
SELECT COUNT(*), MIN(flood_start), MAX(flood_end)
FROM flood_events fe JOIN discharge_stations ds ON ds.station_id = fe.station_id
WHERE ds.station_name ILIKE '%{name}%';

-- Zone flood analysis
SELECT status, COUNT(*) FROM zone_flood_analysis
WHERE zone_id ~ '{pattern}' GROUP BY status;

-- Pixels
SELECT COUNT(*), COUNT(elevation), COUNT(dist_to_river_m)
FROM pixels_static WHERE zone_id ~ '{pattern}';

-- S1 pixel flood labels
SELECT COUNT(*) FROM pixel_flooded pf
JOIN pixels_static ps ON ps.pixel_id = pf.pixel_id
WHERE ps.zone_id ~ '{pattern}';
```

Format results as a concise table. Flag any step where data is missing.

---

## Sub-command: `run-step`

If the user specifies a step name, look it up in the table below and print the
exact command to run (with `--test` as the first suggestion). Ask for the zone
pattern / river name if not already provided.

| step name          | script                              |
|--------------------|-------------------------------------|
| `dfo-ingest`       | `agents/dfo_discharge_ingest.py`    |
| `define-zones`     | `agents/define_study_zones.py`      |
| `flood-calc`       | `agents/calculate_total_flood.py`   |
| `select-pixels`    | `agents/select_study_pixels.py`     |
| `s1-flood-pixels`  | `agents/ingest_s1_flood_pixels.py`  |

---

## General behaviour

- Always check the DB before suggesting scripts — never re-run a step that has
  already populated data unless the user explicitly asks.
- Prefer `--test` for any script the user hasn't run before in this session.
- Use `%(name)s`-style SQL params if writing any queries; never f-strings in SQL.
- If GEE auth is needed, tell the user to run:
  `! earthengine authenticate --project foundation-flood`
- If asked about a river not yet in the DB and the DFO station ID is unknown,
  suggest the user check the DFO Flood Observatory station list at:
  `https://floodobservatory.colorado.edu/wiki/DischargeFromSpace_Tab`
