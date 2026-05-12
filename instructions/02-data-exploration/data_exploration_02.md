# 02_data_exploration_pt2.ipynb
## Purpose
Output `agents/notebooks/02_data_exploration_pt2.py` (Colab). Start at Cell 7.
Goals: (1) Add rivers via HydroSHEDS (2) Collect agri pixels per study zone (3) Dry/wet Sentinel-1 representations.

## Notebook state
`stations_full` DataFrame: station_id, dfo_station_id, station_name, lat, lon, country, continent, total_events, cat1–5_events.
Filter applied: `country=='Pakistan'`, exclude `dfo_station_id=='000103'`.

## DB schema (relevant tables)
- `discharge_stations` — station_id (uuid), geom (Point), river_id (null → populate)
- `rivers` — river_id (uuid PK), river_name, geom (Line), river_source_metadata_id → FK to data_sources
- `study_zones` — zone_id (text PK), station_id FK, river_id FK, geom (5×5km polygon), centroid, zone_set, position, zone_size_m, generation_method, distance_m_along_river

study_zones are 5×5km boxes centred on each discharge_station, positioned along the river.

## Cells
**Cell 7 — Rivers (HydroSHEDS)**
- GEE: snap each station lat/lon to nearest HydroSHEDS river segment (≤2km)
- Extract LineString ±50km of station, store in `rivers`, write river_id back to `discharge_stations`
- Use `ON CONFLICT DO UPDATE`. Print river_name + station matched.

**Cell 8 — Study zones**
- Generate 5×5km polygon centred on station geom
- Compute distance_m_along_river from river start node
- Insert into `study_zones` (zone_set='initial', generation_method='centred_on_station')
- ON CONFLICT DO UPDATE.

**Cell 9 — Agri pixels (Goal 2)**
- GEE product: ESA WorldCover 10m (`ESA/WorldCover/v200`) — class 40 = cropland
- Per study_zone geom, sample cropland pixel centroids
- Print pixel count per zone.

**Cell 10 — Dry/wet Sentinel-1 (Goal 3)**
- Dry: mean VV composite, June–August (low discharge months for Pakistan)
- Wet: mean VV composite, July–September flood_events where category ≥ 2
- Clip to study_zone geom, export both as arrays
- Print mean VV dry vs wet per zone.

## Constraints
- `ON CONFLICT DO UPDATE` on all inserts (idempotent)
- `--test` flag: run Cell 9–10 on first station only
- Print progress per station
- Skip stations with no HydroSHEDS match (warn, continue)