# ============================================================
# CELL 0 — Install dependencies
# ============================================================

# %%
!pip install earthengine-api shapely


# ============================================================
# CELL 7 — Rivers (HydroSHEDS via GEE)
# ============================================================

# %%
import sys
import uuid
import json
import psycopg2
import ee
from shapely.geometry import shape

TEST = '--test' in sys.argv

# stations_full and CONN_STRING inherited from Cell 2
pak_stations = stations_full[
    (stations_full['country'] == 'Pakistan') &
    (stations_full['dfo_station_id'] != '000103')
].copy().reset_index(drop=True)

if TEST:
    pak_stations = pak_stations.head(1)

print(f"TEST={TEST} | Processing {len(pak_stations)} Pakistan stations")

ee.Authenticate()
ee.Initialize()

rivers_fc = ee.FeatureCollection('WWF/HydroSHEDS/v1/FreeFlowingRivers')

with psycopg2.connect(CONN_STRING) as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_id FROM data_sources
            WHERE source_name ILIKE '%%hydrosheds%%'
            LIMIT 1
        """)
        row = cur.fetchone()
        HYDROSHEDS_SOURCE_ID = row[0] if row else None

matched_rows = []

with psycopg2.connect(CONN_STRING) as conn:
    with conn.cursor() as cur:
        for _, srow in pak_stations.iterrows():
            station_id = str(srow['station_id'])
            point    = ee.Geometry.Point([float(srow['lon']), float(srow['lat'])])
            buf_2km  = point.buffer(2000)
            buf_50km = point.buffer(50000)

            nearby = rivers_fc.filterBounds(buf_2km)
            if nearby.size().getInfo() == 0:
                print(f"  WARN: no HydroSHEDS match within 2 km for {srow['station_name']}, skipping")
                continue

            # Segment with largest upstream area = main channel
            segment = nearby.sort('UPLAND_SKM', False).first()
            props = segment.getInfo()['properties']
            river_name = (
                props.get('RIV_NAME_EN')
                or props.get('RIV_NAME')
                or f"River_{props.get('MAIN_RIV', station_id[:8])}"
            )

            clipped_geojson = segment.geometry().intersection(buf_50km).getInfo()
            shp = shape(clipped_geojson)

            if shp.is_empty:
                print(f"  WARN: empty clipped geometry for {srow['station_name']}, skipping")
                continue

            # Ensure single LineString (take longest part if multi)
            if hasattr(shp, 'geoms'):
                shp = max(shp.geoms, key=lambda g: g.length)

            # Deterministic river_id so ON CONFLICT DO UPDATE works on re-runs
            river_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"hydrosheds_{station_id}"))

            cur.execute("""
                INSERT INTO rivers (river_id, river_name, geom, river_source_metadata_id)
                VALUES (
                    %(river_id)s::uuid,
                    %(river_name)s,
                    ST_GeomFromText(%(wkt)s, 4326),
                    %(source_id)s
                )
                ON CONFLICT (river_id) DO UPDATE
                    SET river_name               = EXCLUDED.river_name,
                        geom                     = EXCLUDED.geom,
                        river_source_metadata_id = EXCLUDED.river_source_metadata_id
            """, {
                'river_id':   river_id,
                'river_name': river_name,
                'wkt':        shp.wkt,
                'source_id':  HYDROSHEDS_SOURCE_ID,
            })

            cur.execute("""
                UPDATE discharge_stations
                   SET river_id = %(river_id)s::uuid
                 WHERE station_id = %(station_id)s::uuid
            """, {'river_id': river_id, 'station_id': station_id})

            conn.commit()

            matched_rows.append({
                'station_id':     station_id,
                'dfo_station_id': srow['dfo_station_id'],
                'station_name':   srow['station_name'],
                'river_id':       river_id,
                'river_name':     river_name,
            })
            print(f"  Matched: {srow['station_name']} → {river_name}")

matched_df = pd.DataFrame(matched_rows)
print(f"\nDone: {len(matched_df)}/{len(pak_stations)} stations matched to rivers")


# ============================================================
# CELL 8 — Study zones (5×5 km centred on station)
# ============================================================

# %%
with psycopg2.connect(CONN_STRING) as conn:
    with conn.cursor() as cur:
        for _, mrow in matched_df.iterrows():
            station_id = mrow['station_id']
            river_id   = mrow['river_id']
            zone_id    = f"{mrow['dfo_station_id']}_initial"

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
                            ST_X(ST_Transform(ds.geom::geometry, 32642)) - 2500,
                            ST_Y(ST_Transform(ds.geom::geometry, 32642)) - 2500,
                            ST_X(ST_Transform(ds.geom::geometry, 32642)) + 2500,
                            ST_Y(ST_Transform(ds.geom::geometry, 32642)) + 2500,
                            32642
                        ),
                        4326
                    ),
                    ds.geom::geometry,
                    'initial',
                    'centre',
                    5000,
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
                        river_id               = EXCLUDED.river_id,
                        distance_m_along_river = EXCLUDED.distance_m_along_river
            """, {
                'zone_id':    zone_id,
                'station_id': station_id,
                'river_id':   river_id,
            })
            conn.commit()
            print(f"  Inserted study zone: {zone_id}")

print(f"\nDone: {len(matched_df)} study zones written")


# ============================================================
# CELL 9 — Agri pixels (ESA WorldCover 10 m, class 40 = cropland)
# ============================================================

# %%
station_ids = matched_df['station_id'].tolist()

with psycopg2.connect(CONN_STRING) as conn:
    zones_df = pd.read_sql("""
        SELECT
            sz.zone_id,
            sz.station_id::text,
            ds.station_name,
            ST_AsGeoJSON(sz.geom) AS geom_json
        FROM study_zones sz
        JOIN discharge_stations ds USING (station_id)
        WHERE sz.zone_set = 'initial'
          AND sz.station_id = ANY(%(ids)s::uuid[])
        ORDER BY ds.station_name
    """, conn, params={'ids': station_ids})

if TEST:
    zones_df = zones_df.head(1)

worldcover = ee.Image('ESA/WorldCover/v200').select('Map')

print("Cropland pixel counts per study zone:")
for _, zone in zones_df.iterrows():
    zone_geom = ee.Geometry(json.loads(zone['geom_json']))
    cropland_mask = worldcover.eq(40).selfMask()
    samples = cropland_mask.sample(region=zone_geom, scale=10, geometries=False)
    pixel_count = samples.size().getInfo()
    print(f"  {zone['station_name']} ({zone['zone_id']}): {pixel_count} cropland pixels")


# ============================================================
# CELL 10 — Dry/wet Sentinel-1 VV representations
# ============================================================

# %%
with psycopg2.connect(CONN_STRING) as conn:
    flood_events = pd.read_sql("""
        SELECT station_id::text, flood_start, flood_end
        FROM flood_events
        WHERE station_id = ANY(%(ids)s::uuid[])
          AND max_category >= 2
          AND EXTRACT(MONTH FROM flood_start) BETWEEN 7 AND 9
          AND detection_method = 'threshold_exceedance_7d'
        ORDER BY flood_start
    """, conn, params={'ids': station_ids})

flood_events['flood_start'] = pd.to_datetime(flood_events['flood_start'])
flood_events['flood_end']   = pd.to_datetime(flood_events['flood_end'])

s1 = (
    ee.ImageCollection('COPERNICUS/S1_GRD')
    .filter(ee.Filter.eq('instrumentMode', 'IW'))
    .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
    .select('VV')
)

# Reload study zones (in case Cell 9 was skipped)
with psycopg2.connect(CONN_STRING) as conn:
    zones_df = pd.read_sql("""
        SELECT
            sz.zone_id,
            sz.station_id::text,
            ds.station_name,
            ST_AsGeoJSON(sz.geom) AS geom_json
        FROM study_zones sz
        JOIN discharge_stations ds USING (station_id)
        WHERE sz.zone_set = 'initial'
          AND sz.station_id = ANY(%(ids)s::uuid[])
        ORDER BY ds.station_name
    """, conn, params={'ids': station_ids})

if TEST:
    zones_df = zones_df.head(1)

print("Mean VV (dB) dry vs wet per study zone:")
for _, zone in zones_df.iterrows():
    zone_geom = ee.Geometry(json.loads(zone['geom_json']))

    dry_stats = (
        s1.filter(ee.Filter.calendarRange(6, 8, 'month'))
        .mean()
        .reduceRegion(reducer=ee.Reducer.mean(), geometry=zone_geom,
                      scale=10, maxPixels=1e7)
        .getInfo()
    )

    station_floods = flood_events[flood_events['station_id'] == zone['station_id']]
    wet_vv = None
    if not station_floods.empty:
        wet_filters = [
            ee.Filter.date(
                r['flood_start'].strftime('%Y-%m-%d'),
                r['flood_end'].strftime('%Y-%m-%d'),
            )
            for _, r in station_floods.iterrows()
        ]
        wet_stats = (
            s1.filter(ee.Filter.Or(*wet_filters))
            .mean()
            .reduceRegion(reducer=ee.Reducer.mean(), geometry=zone_geom,
                          scale=10, maxPixels=1e7)
            .getInfo()
        )
        wet_vv = wet_stats.get('VV')

    dry_vv = dry_stats.get('VV')
    dry_str = f"{dry_vv:.2f} dB" if dry_vv is not None else "N/A"
    wet_str = f"{wet_vv:.2f} dB" if wet_vv is not None else "N/A"
    print(f"  {zone['station_name']} ({zone['zone_id']}): dry={dry_str}  wet={wet_str}")
