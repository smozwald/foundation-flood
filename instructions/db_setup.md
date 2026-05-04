# Task: Database schema checker — agents/db_setup.py

## Purpose
A CLI script that inspects the live Supabase schema and brings it into
alignment with the project specification. Safe to re-run at any time.

## Flags
- `--check` — read-only; print what is missing or wrong, exit 1 if issues found
- default (no flag) — apply all missing tables, columns, extensions, indexes,
  and FK constraints, then re-check and report

## Connection
Load `SUPABASE_CONN_STRING` from `.env` via python-dotenv.
Use `with psycopg2.connect(CONN_STRING) as conn`.

## Extensions required
- `postgis`
- `vector`
- `uuid-ossp`

Install with `CREATE EXTENSION IF NOT EXISTS` if missing.

## Table creation order
Tables must be created in dependency order to satisfy FK constraints:

1. `data_sources`
2. `rivers`
3. `study_zones`
4. `pixels_static`
5. `ts_sentinel1`
6. `ts_sentinel2`
7. `ts_meteo`
8. `labels_flooding`
9. `pixel_embeddings`
10. `discharge_stations`
11. `discharge_ts`
12. `flood_thresholds`
13. `flood_events`

## Target schema

### `data_sources`
```sql
source_id        uuid PRIMARY KEY DEFAULT gen_random_uuid()
product_name     text NOT NULL
methodology_desc text
resolution_m     float
version_tag      text
```

### `rivers`
```sql
river_id                 uuid PRIMARY KEY DEFAULT gen_random_uuid()
river_name               text NOT NULL
geom                     geometry(LineString, 4326)
river_source_metadata_id uuid REFERENCES data_sources(source_id)
```

### `study_zones`
```sql
zone_id              text PRIMARY KEY          -- e.g. '000257_3km_1'
river_id             uuid REFERENCES rivers(river_id)
station_id           uuid                      -- FK wired after discharge_stations exists
geom                 geometry(Polygon, 4326)
centroid             geometry(Point, 4326)
zone_set             text                      -- e.g. '3km'
position             smallint                  -- 1=at_station, 2=downstream_1, 3=downstream_2
zone_size_m          integer                   -- tile edge in metres, e.g. 3000
generation_method    text                      -- e.g. 'hydrosheds_centreline_step'
distance_m_along_river float                   -- metres along centreline from station
generated_at         timestamptz DEFAULT now()
```
Indexes: GIST on `geom`, btree on `river_id`.

### `pixels_static`
```sql
pixel_id         text PRIMARY KEY
zone_id          text REFERENCES study_zones(zone_id)
is_agri          boolean
agri_metadata_id uuid REFERENCES data_sources(source_id)
elevation        float
slope            float
twi              float
spi              float
curvature        float
dist_to_river_m  float
topo_metadata_id uuid REFERENCES data_sources(source_id)
```
Index: btree on `zone_id`.

### `ts_sentinel1`
```sql
pixel_id        text REFERENCES pixels_static(pixel_id)
obs_date        date NOT NULL
vv              float
vh              float
incidence_angle float
metadata        jsonb
PRIMARY KEY (pixel_id, obs_date)
```

### `ts_sentinel2`
```sql
pixel_id   text REFERENCES pixels_static(pixel_id)
obs_date   date NOT NULL
b2..b12    float (b2, b3, b4, b5, b6, b7, b8, b8a, b11, b12)
cloud_prob float
metadata   jsonb
PRIMARY KEY (pixel_id, obs_date)
```

### `ts_meteo`
```sql
pixel_id          text REFERENCES pixels_static(pixel_id)
obs_date          date NOT NULL
precip_mm         float
temp_max          float
temp_min          float
source_station_id text
PRIMARY KEY (pixel_id, obs_date)
```

### `labels_flooding`
```sql
pixel_id           text REFERENCES pixels_static(pixel_id)
obs_date           date NOT NULL
is_flooded         boolean
algorithm_version  text
method_metadata_id uuid REFERENCES data_sources(source_id)
event_id           uuid    -- FK to flood_events, wired after that table exists
PRIMARY KEY (pixel_id, obs_date)
```

### `pixel_embeddings`
```sql
pixel_id     text REFERENCES pixels_static(pixel_id)
window_start date NOT NULL
model_name   text NOT NULL
embedding    vector(128)
PRIMARY KEY (pixel_id, window_start, model_name)
```

### `discharge_stations`
```sql
station_id     uuid PRIMARY KEY DEFAULT gen_random_uuid()
dfo_station_id text UNIQUE NOT NULL
station_name   text
geom           geometry(Point, 4326) NOT NULL
zone_id        text REFERENCES study_zones(zone_id)        -- nullable until GEE step
river_id       uuid REFERENCES rivers(river_id)            -- nullable until GEE step
source_id      uuid REFERENCES data_sources(source_id)
record_start   date
record_end     date
```
Indexes: GIST on `geom`.

### `discharge_ts`
```sql
station_id                  uuid REFERENCES discharge_stations(station_id)
obs_date                    date NOT NULL
discharge_m3s               float
discharge_anomaly_pct       float
threshold_exceeded_category smallint CHECK (threshold_exceeded_category BETWEEN 1 AND 5)
qc_flag                     text
source_id                   uuid REFERENCES data_sources(source_id)
PRIMARY KEY (station_id, obs_date)
```
Indexes: btree on `(station_id, obs_date DESC)`, partial index on `station_id` WHERE
`threshold_exceeded_category IS NOT NULL`.

### `flood_thresholds`
```sql
threshold_id            uuid PRIMARY KEY DEFAULT gen_random_uuid()
station_id              uuid REFERENCES discharge_stations(station_id) NOT NULL
category                smallint NOT NULL CHECK (category BETWEEN 1 AND 5)
return_period_label     text NOT NULL
discharge_threshold_m3s float NOT NULL
derived_from            text DEFAULT 'DFO_station_page'
valid_from              date
valid_to                date
UNIQUE (station_id, category)
```

### `flood_events`
```sql
event_id           uuid PRIMARY KEY DEFAULT gen_random_uuid()
station_id         uuid REFERENCES discharge_stations(station_id) NOT NULL
river_id           uuid REFERENCES rivers(river_id)        -- nullable until GEE step
flood_start        date NOT NULL
flood_end          date NOT NULL
duration_days      integer                                  -- computed on insert, not GENERATED
peak_discharge_m3s float
peak_date          date
max_category       smallint CHECK (max_category BETWEEN 1 AND 5)
dfo_archive_id     text
detection_method   text DEFAULT 'threshold_exceedance'
notes              text
CONSTRAINT valid_dates CHECK (flood_end >= flood_start)
```
Indexes: btree on `station_id`, btree on `(flood_start, flood_end)`.

## Deferred FK wiring
Two FKs cannot be created at initial table creation time (circular / ordering
issue). Add them after all tables exist:

1. `labels_flooding.event_id → flood_events(event_id)`
2. `study_zones.station_id → discharge_stations(station_id)`

Check for existing constraint name before adding to keep the script idempotent.

## Behaviour
- Use `CREATE TABLE IF NOT EXISTS` for new tables.
- Use `ALTER TABLE … ADD COLUMN IF NOT EXISTS` for missing columns.
- For missing FK constraints: check `information_schema.table_constraints` by
  name before adding.
- Print one line per action taken: `Creating table: X`, `Adding column: X.Y`, etc.
- If `--check` mode and no issues: print `Schema OK` and exit 0.
- If `--check` mode with issues: list them and exit 1.
- Default mode: apply, re-check, exit 0 on clean, exit 1 if anything remains.

## Code standards (from CLAUDE.md)
- All SQL uses `%(name)s` psycopg2 placeholders — never f-strings in SQL
- `with psycopg2.connect(CONN_STRING) as conn`
- Exit code 0 on success, 1 on failure
