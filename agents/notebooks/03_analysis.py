"""
03_analysis.py — Corrected queries and helpers for notebook 03_initial_model.ipynb.

Paste individual functions / query strings into Colab cells as needed.
Run standalone:  python agents/notebooks/03_analysis.py

Column name corrections vs the original notebook:
  zone_flood_analysis:
    zfa.event_id          → zfa.flood_event_id
    zfa.percentage_flooded → zfa.flooded_agri_pct
    zfa.scene_date         → zfa.wet_scene_date
  pixel_flooded:
    pf.event_id            → pf.flood_event_id
    pf.flooded             → pf.is_flooded
    pf.vv_flood / vh_flood → pf.vv_change_db  (only VV change stored)
"""

import os
import sys

import psycopg2
import pandas as pd

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_conn():
    """Use userdata in Colab; fall back to env var locally."""
    try:
        from google.colab import userdata
        conn_str = userdata.get('SUPABASE_CONN_STRING')
    except Exception:
        from dotenv import load_dotenv
        load_dotenv()
        conn_str = os.environ.get('SUPABASE_CONN_STRING')
    if not conn_str:
        sys.exit('ERROR: SUPABASE_CONN_STRING not set')
    return psycopg2.connect(conn_str)


def read_sql(sql, conn, params=None):
    """pd.read_sql wrapper that avoids the psycopg2 UserWarning."""
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


# ---------------------------------------------------------------------------
# CELL 4 — Phase One status counts
# ---------------------------------------------------------------------------

STATUS_QUERIES = {
    'discharge_stations':          'SELECT COUNT(*) FROM discharge_stations',
    'discharge_ts':                'SELECT COUNT(*) FROM discharge_ts',
    'flood_events':                'SELECT COUNT(*) FROM flood_events',
    'study_zones':                 'SELECT COUNT(*) FROM study_zones',
    "zone_flood_analysis (SUCCESS)":
        "SELECT COUNT(*) FROM zone_flood_analysis WHERE status = 'SUCCESS'",
    'pixels_static':               'SELECT COUNT(*) FROM pixels_static',
    'ts_sentinel1':                'SELECT COUNT(*) FROM ts_sentinel1',
    'pixel_flooded':               'SELECT COUNT(*) FROM pixel_flooded',
}


def load_status(conn):
    rows = []
    with conn.cursor() as cur:
        for label, sql in STATUS_QUERIES.items():
            cur.execute(sql)
            rows.append({'Table': label, 'Rows': cur.fetchone()[0]})
    df = pd.DataFrame(rows)
    df['Status'] = df['Rows'].apply(lambda n: '✓ complete' if n > 0 else '— empty')
    return df


# ---------------------------------------------------------------------------
# CELL 5 — Zone breakdown (corrected column names)
# ---------------------------------------------------------------------------

ZONE_SQL = """
    SELECT
        zfa.zone_id,
        zfa.flood_event_id,
        fe.flood_start,
        fe.flood_end,
        fe.max_category,
        zfa.status,
        zfa.flooded_agri_pct        AS percentage_flooded,
        zfa.wet_scene_date          AS scene_date
    FROM zone_flood_analysis zfa
    JOIN flood_events fe ON fe.event_id = zfa.flood_event_id
    WHERE zfa.zone_id ~ %(pat)s
    ORDER BY fe.flood_start
"""

PIXELS_SQL = """
    SELECT
        ps.pixel_id,
        ST_Y(ps.geom::geometry) AS lat,
        ST_X(ps.geom::geometry) AS lon,
        ps.elevation,
        ps.slope,
        ps.dist_to_river_m
    FROM pixels_static ps
    WHERE ps.zone_id ~ %(pat)s
"""

FLOODED_SQL = """
    SELECT
        pf.pixel_id,
        pf.flood_event_id,
        pf.is_flooded               AS flooded,
        pf.vv_change_db,
        pf.wet_obs_date
    FROM pixel_flooded pf
    JOIN pixels_static ps ON ps.pixel_id = pf.pixel_id
    WHERE ps.zone_id ~ %(pat)s
"""


def load_zone_data(conn, zone_pattern='000099_initial'):
    p = {'pat': zone_pattern}
    zone_df    = read_sql(ZONE_SQL,    conn, p)
    pix_df     = read_sql(PIXELS_SQL,  conn, p)
    flooded_df = read_sql(FLOODED_SQL, conn, p)
    zone_df['flood_start'] = pd.to_datetime(zone_df['flood_start'])
    zone_df['flood_end']   = pd.to_datetime(zone_df['flood_end'])
    return zone_df, pix_df, flooded_df


# ---------------------------------------------------------------------------
# CELL 6 — Per-event flood summary (works whether pixel_flooded is empty or not)
# ---------------------------------------------------------------------------

def event_flood_summary(zone_df, flooded_df):
    """
    If pixel_flooded is populated: compute per-event pixel flood rate.
    Otherwise fall back to zone-level flooded_agri_pct.
    """
    success = zone_df[zone_df['status'] == 'SUCCESS'].copy()

    if len(flooded_df) == 0:
        print('pixel_flooded is empty — using zone-level flooded_agri_pct')
        return success[['flood_start', 'max_category', 'percentage_flooded']].rename(
            columns={'percentage_flooded': 'pct_flooded'}
        )

    agg = (
        flooded_df.groupby('flood_event_id')['flooded']
        .agg(total='count', flooded_n='sum')
        .assign(pct_flooded=lambda d: 100 * d['flooded_n'] / d['total'])
        .reset_index()
    )
    merged = success.merge(agg, on='flood_event_id', how='left')
    return merged[['flood_start', 'max_category', 'total', 'flooded_n', 'pct_flooded']]


# ---------------------------------------------------------------------------
# CELL 9 / 10 — per-event pixel labels for scatter plots
# ---------------------------------------------------------------------------

def pixels_for_event(pix_df, flooded_df, flood_event_id):
    """
    Returns pix_df with a 'flooded' bool column for the given event.
    If flooded_df is empty, all pixels get flooded=False.
    """
    if len(flooded_df) == 0:
        pix_df = pix_df.copy()
        pix_df['flooded'] = False
        return pix_df

    ev = flooded_df[flooded_df['flood_event_id'] == flood_event_id][['pixel_id', 'flooded']]
    merged = pix_df.merge(ev, on='pixel_id', how='left')
    merged['flooded'] = merged['flooded'].fillna(False)
    return merged


# ---------------------------------------------------------------------------
# CELL 14 — JRC GSW comparison (percentage_flooded → flooded_agri_pct)
# ---------------------------------------------------------------------------

def jrc_comparison_table(zone_df, flooded_df, ee, zone_bbox):
    """
    Returns a DataFrame comparing our Otsu % vs JRC GSW monthly water % per event.
    Pass an initialised ee module and a zone_bbox ee.Geometry.
    """
    success = zone_df[zone_df['status'] == 'SUCCESS'].copy()
    success['flood_start'] = pd.to_datetime(success['flood_start'])

    jrc_results = []
    for _, ev in success.iterrows():
        y = int(ev['flood_start'].year)
        m = int(ev['flood_start'].month)
        try:
            jrc = (
                ee.ImageCollection('JRC/GSW1_4/MonthlyHistory')
                .filter(ee.Filter.calendarRange(y, y, 'year'))
                .filter(ee.Filter.calendarRange(m, m, 'month'))
                .first()
            )
            stats = jrc.eq(2).reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=zone_bbox,
                scale=30
            ).getInfo()
            pct = round((stats.get('water') or 0) * 100, 2)
        except Exception as e:
            pct = None
        jrc_results.append({'flood_event_id': ev['flood_event_id'], 'jrc_pct': pct})

    jrc_df = pd.DataFrame(jrc_results)
    compare = success[['flood_start', 'max_category', 'percentage_flooded', 'flood_event_id']].merge(
        jrc_df, on='flood_event_id', how='left'
    )
    compare = compare.rename(columns={
        'percentage_flooded': 'our_sar_otsu_pct',
    })
    return compare[['flood_start', 'max_category', 'our_sar_otsu_pct', 'jrc_pct']]


# ---------------------------------------------------------------------------
# CELL 9 — Flooded vs unflooded pixels per event on S2 basemap
# ---------------------------------------------------------------------------
# Paste from here into Colab Cell 9 (after Cell 5 has run).
# Requires: zone_df, pix_df, flooded_df, fetch_s2_thumbnail, ee, pd, plt

success_events = zone_df[zone_df['status'] == 'SUCCESS'].copy()
success_events['flood_start'] = pd.to_datetime(success_events['flood_start'])
success_events = success_events.reset_index(drop=True)

if len(pix_df) == 0:
    print('No pixel data — skipping visualisation')
else:
    lat_c = pix_df['lat'].mean()
    lon_c = pix_df['lon'].mean()

    ref_date = success_events.iloc[0]['flood_start'].strftime('%Y-%m-%d') if len(success_events) > 0 else '2023-01-01'
    try:
        bg_img, region_info = fetch_s2_thumbnail(lat_c, lon_c, ref_date)
        coords = region_info['coordinates'][0]
        lon_min = min(c[0] for c in coords)
        lon_max = max(c[0] for c in coords)
        lat_min = min(c[1] for c in coords)
        lat_max = max(c[1] for c in coords)
        has_bg = True
        print(f'S2 basemap loaded ({lon_min:.4f},{lat_min:.4f}) → ({lon_max:.4f},{lat_max:.4f})')
    except Exception as e:
        print(f'S2 basemap unavailable ({e}) — plotting without background')
        has_bg = False
        lon_min, lon_max = pix_df['lon'].min() - 0.01, pix_df['lon'].max() + 0.01
        lat_min, lat_max = pix_df['lat'].min() - 0.01, pix_df['lat'].max() + 0.01

    n_events = len(success_events)
    ncols = 2
    nrows = (n_events + ncols - 1) // ncols

    fig, axes = plt.subplots(nrows, ncols, figsize=(14, 7 * nrows))
    if n_events == 1:
        axes = [[axes]]
    elif nrows == 1:
        axes = [axes]

    for idx, (_, ev) in enumerate(success_events.iterrows()):
        row, col = divmod(idx, ncols)
        ax = axes[row][col]

        if has_bg:
            ax.imshow(bg_img, extent=[lon_min, lon_max, lat_min, lat_max], aspect='auto', alpha=0.6)

        ev_flooded = flooded_df[flooded_df['flood_event_id'] == ev.get('flood_event_id')] if len(flooded_df) > 0 else pd.DataFrame()

        if len(ev_flooded) > 0:
            merged = pix_df.merge(ev_flooded[['pixel_id', 'flooded']], on='pixel_id', how='left')
            merged['flooded'] = merged['flooded'].fillna(False)
            unflooded = merged[~merged['flooded']]
            flooded   = merged[merged['flooded']]
            ax.scatter(unflooded['lon'], unflooded['lat'], s=2, c='#d73027', alpha=0.6, label='Unflooded agri')
            ax.scatter(flooded['lon'],   flooded['lat'],   s=2, c='#4575b4', alpha=0.8,  label='Flooded agri')
            n_fl = int(merged['flooded'].sum())
            pct_str = f"{100 * n_fl / len(merged):.1f}% flooded ({n_fl}/{len(merged)} px)"
        else:
            ax.scatter(pix_df['lon'], pix_df['lat'], s=2, c='#999999', alpha=0.5, label='Agri pixels')
            pct_str = f"{ev['percentage_flooded']:.1f}% flooded (zone-level)"

        scene = str(ev['scene_date']) if pd.notnull(ev.get('scene_date')) else str(ev['flood_start'].date())
        ax.set_title(
            f"{ev['flood_start'].strftime('%b %Y')} | Cat {int(ev['max_category'])}\n"
            f"Scene: {scene} | {pct_str}",
            fontsize=8
        )
        ax.set_xlim(lon_min, lon_max)
        ax.set_ylim(lat_min, lat_max)
        ax.tick_params(labelsize=7)
        if idx == 0:
            ax.legend(loc='upper right', fontsize=6, markerscale=4)

    for idx in range(n_events, nrows * ncols):
        row, col = divmod(idx, ncols)
        axes[row][col].set_visible(False)

    plt.suptitle('Flood event maps — zone 000099_initial (Chenab River, Pakistan)', fontsize=11, y=1.01)
    plt.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# CELL 10 — Method comparison: Otsu vs fixed-threshold SAR vs JRC GSW / EMS
# ---------------------------------------------------------------------------
# Panel 1: Our Otsu (stored is_flooded from pixel_flooded)
# Panel 2: Fixed -3 dB threshold applied to stored vv_change_db — no GEE re-fetch
# Panel 3: JRC GSW Monthly History from GEE (Landsat water mask for flood month).
#          Copernicus EMS portal does not expose stable download URLs, so JRC GSW
#          is used as the independent reference. If you manually upload an EMS
#          shapefile to temp_images/ it will be used instead.
# Paste from here into Colab Cell 10 (after Cell 5 has run).

import geopandas as gpd
from shapely.geometry import Point

SAR_CHANGE_THRESHOLD_DB = -3.0

# Optional manual override — upload EMS "Observed Event" shapefile to Colab:
#   2022 (EMSR629): https://emergency.copernicus.eu/mapping/list-of-components/EMSR629
#   2025 (EMSR838): https://emergency.copernicus.eu/mapping/list-of-components/EMSR838
EMS_PATHS = {
    '2022': 'temp_images/emsr629_observed_event.shp',
    '2025': 'temp_images/emsr838_observed_event.shp',
}

pad = 0.003
lon_min = pix_df['lon'].min() - pad;  lon_max = pix_df['lon'].max() + pad
lat_min = pix_df['lat'].min() - pad;  lat_max = pix_df['lat'].max() + pad


def _fixed_threshold_labels(pix_df, ev_flooded, threshold_db=SAR_CHANGE_THRESHOLD_DB):
    """Apply a fixed dB threshold to the stored vv_change_db values (no GEE needed)."""
    merged = pix_df[['pixel_id']].merge(
        ev_flooded[['pixel_id', 'vv_change_db']], on='pixel_id', how='left'
    )
    labels = (merged['vv_change_db'] < threshold_db).fillna(False).reset_index(drop=True)
    n_valid = ev_flooded['vv_change_db'].notna().sum()
    print(f"  Fixed threshold: {labels.sum()} flooded / {len(labels)} px "
          f"({n_valid} have vv_change_db)")
    return labels


def _ems_labels(pix_df, shp_path):
    """Point-in-polygon against EMS observed-event shapefile. Returns None if missing."""
    if not os.path.exists(shp_path):
        return None, None
    gdf = gpd.read_file(shp_path).to_crs('EPSG:4326')
    # Try standard EMS field filters; fall back to all polygons if nothing matches
    filtered = pd.DataFrame()
    for col in ['obj_type', 'symbology', 'TYPE']:
        if col in gdf.columns:
            filtered = gdf[gdf[col].str.lower().str.contains('flood|water|inundat', na=False)]
            if len(filtered):
                break
    if len(filtered) == 0:
        print(f"  EMS: no flood filter matched — using all {len(gdf)} polygons")
        filtered = gdf
    else:
        print(f"  EMS: {len(filtered)} flood polygons from {os.path.basename(shp_path)}")
    union = filtered.geometry.union_all()
    pts   = [Point(lon, lat) for lon, lat in zip(pix_df['lon'], pix_df['lat'])]
    labels = pd.Series([union.contains(p) for p in pts], index=pix_df.index)
    return labels, f'Copernicus EMS ({os.path.basename(shp_path)[:7].upper()})'


def _jrc_labels(pix_df, year, month, ee):
    """JRC GSW Monthly History — value 2 = water observed that month.
    Dataset covers 1984–2021; raises ValueError for later years."""
    col = (ee.ImageCollection('JRC/GSW1_4/MonthlyHistory')
           .filter(ee.Filter.calendarRange(year, year, 'year'))
           .filter(ee.Filter.calendarRange(month, month, 'month')))
    if col.size().getInfo() == 0:
        raise ValueError(
            f"JRC GSW has no data for {year}/{month:02d} — dataset ends 2021. "
            "No optical reference available (monsoon cloud cover also makes optical "
            "unreliable for Pakistan flood events; SAR is the right tool here)."
        )
    jrc = col.first()
    pts = ee.FeatureCollection([
        ee.Feature(ee.Geometry.Point([float(r['lon']), float(r['lat'])]),
                   {'pixel_id': r['pixel_id']})
        for _, r in pix_df.iterrows()
    ])
    sampled = jrc.sampleRegions(collection=pts, scale=30, geometries=False).getInfo()
    result  = {f['properties']['pixel_id']: f['properties'].get('water', 0) == 2
               for f in sampled['features']}
    labels  = pix_df['pixel_id'].map(result).fillna(False)
    print(f"  JRC GSW {year}/{month:02d}: {labels.sum()} water pixels / {len(labels)}")
    return labels


def _iou(a, b):
    a, b = a.astype(bool).values, b.astype(bool).values
    inter = (a & b).sum()
    union = (a | b).sum()
    return inter / union if union > 0 else float('nan')


# Find best SUCCESS event per year
events_to_compare = {}
for year in [2022, 2025]:
    matches = zone_df[
        (zone_df['flood_start'].dt.year == year) &
        (zone_df['status'] == 'SUCCESS')
    ].sort_values('percentage_flooded', ascending=False)
    if len(matches):
        events_to_compare[str(year)] = matches.iloc[0]
        print(f"{year}: flood_start={matches.iloc[0]['flood_start'].date()}  "
              f"scene={str(matches.iloc[0]['scene_date'])}  "
              f"zone-level={matches.iloc[0]['percentage_flooded']:.1f}%")
    else:
        print(f"{year}: no SUCCESS event found")


n_events = len(events_to_compare)
fig, axes = plt.subplots(n_events, 3, figsize=(18, 6 * n_events))
if n_events == 1:
    axes = [axes]

for row_i, (year_label, ev) in enumerate(events_to_compare.items()):
    ev_flooded = flooded_df[flooded_df['flood_event_id'] == ev['flood_event_id']]
    merged = pix_df.merge(ev_flooded[['pixel_id', 'flooded']], on='pixel_id', how='left')
    merged['flooded'] = merged['flooded'].fillna(False)
    our   = merged['flooded'].reset_index(drop=True)
    scene = str(ev['scene_date']) if pd.notnull(ev['scene_date']) else '—'

    print(f'\n{year_label}: fixed-threshold SAR ...')
    sar_fixed = _fixed_threshold_labels(pix_df, ev_flooded)

    print(f'{year_label}: reference layer ...')
    ref_labels, ref_title = _ems_labels(pix_df, EMS_PATHS.get(year_label, ''))
    if ref_labels is None:
        year_int  = int(ev['flood_start'].year)
        month_int = int(ev['flood_start'].month)
        try:
            ref_labels = _jrc_labels(pix_df, year_int, month_int, ee)
            ref_title  = f'JRC GSW {year_int}/{month_int:02d} (Landsat)'
        except Exception as e:
            print(f'  JRC GSW failed: {e}')
            ref_title  = 'Reference unavailable'

    panels = [
        ('Our Otsu SAR\n(adaptive threshold)', our, None),
        (f'Fixed {SAR_CHANGE_THRESHOLD_DB} dB threshold\n(same stored vv_change_db)', sar_fixed, our),
        (ref_title, ref_labels, our),
    ]

    for col_i, (title, labels, ref) in enumerate(panels):
        ax = axes[row_i][col_i]
        if labels is None:
            ax.text(0.5, 0.5,
                    'No optical reference available\n\n'
                    'JRC GSW ends 2021; Dynamic World\n'
                    'and Sentinel-2 are cloud-obscured\n'
                    'during Pakistan monsoon.\n\n'
                    'Upload EMS shapefile to\ntemp_images/ to enable this panel.',
                    ha='center', va='center', transform=ax.transAxes, fontsize=8,
                    bbox=dict(boxstyle='round', facecolor='#fff3cd', alpha=0.8))
            ax.set_title(f'{year_label} | {title}', fontsize=9)
            ax.set_xlim(lon_min, lon_max)
            ax.set_ylim(lat_min, lat_max)
            continue

        pct     = 100 * labels.sum() / len(labels)
        iou_str = f'  IoU vs Otsu: {_iou(ref, labels):.3f}' if ref is not None else ''
        fl      = pix_df[labels.values]
        unfl    = pix_df[~labels.values]
        ax.scatter(unfl['lon'], unfl['lat'], s=2, c='#d73027', alpha=0.6,  label='Unflooded')
        ax.scatter(fl['lon'],   fl['lat'],   s=2, c='#4575b4', alpha=0.85, label='Flooded')
        ax.set_title(f'{year_label} | {title}\nScene: {scene} | {pct:.1f}% flooded{iou_str}', fontsize=8)
        ax.set_xlim(lon_min, lon_max)
        ax.set_ylim(lat_min, lat_max)
        ax.tick_params(labelsize=6)
        if col_i == 0 and row_i == 0:
            ax.legend(loc='upper right', fontsize=6, markerscale=3)

plt.suptitle('Flood method comparison — zone 000099_initial (Chenab River)', fontsize=12, y=1.01)
plt.tight_layout()
plt.show()


# ---------------------------------------------------------------------------
# CELL 11 — Load Fields of the World for zone 000099_initial
# ---------------------------------------------------------------------------
# Uses DuckDB to spatial-query just the zone bounding box from the FoTW
# GeoParquet hosted on Source Cooperative — no full-country download needed.
# Paste from here into Colab Cell 11 (after Cell 5 has run).

import subprocess
subprocess.run(['pip', 'install', 'duckdb', 'geopandas', 'shapely'], capture_output=True)
import duckdb
import geopandas as gpd
from shapely.geometry import box

if len(pix_df) == 0:
    print('No pixel data — skipping FoTW')
else:
    lat_c = pix_df['lat'].mean()
    lon_c = pix_df['lon'].mean()
    buf = 0.06  # ~7 km buffer around pixel centroid

    xmin, xmax = lon_c - buf, lon_c + buf
    ymin, ymax = lat_c - buf, lat_c + buf
    print(f'Zone bbox: lon [{xmin:.4f}, {xmax:.4f}]  lat [{ymin:.4f}, {ymax:.4f}]')

    # Global FoTW predictions (2024 + 2025, 3.17B polygons worldwide).
    # Dataset: https://source.coop/ftw/global-data
    # ~1001 parquet files — DuckDB prunes by row-group bbox stats so only
    # files covering the zone are actually read.
    FTW_S3 = 's3://us-west-2.opendata.source.coop/ftw/global-data/predictions/vectors/alpha/results/*.parquet'

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")

    fields_gdf = None
    try:
        # Peek at schema first so we know the geometry column name
        schema_row = con.execute(f"SELECT * FROM read_parquet('{FTW_S3}') LIMIT 1").df()
        print(f'  Schema columns: {list(schema_row.columns)}')

        print(f'Querying FoTW predictions for bbox (may take 30–90 s while DuckDB prunes files) ...')
        result = con.execute(f"""
            SELECT *
            FROM read_parquet('{FTW_S3}')
            WHERE bbox.xmin < {xmax}
              AND bbox.xmax > {xmin}
              AND bbox.ymin < {ymax}
              AND bbox.ymax > {ymin}
        """).df()
        print(f'  Rows returned: {len(result)}')
        if len(result) == 0:
            print('  No fields found in bbox.')
            print('  The global layer covers all countries — check bbox coordinates above.')
        else:
            # GeoParquet stores geometry as WKB bytes in a column named 'geometry'
            geom_col = next((c for c in result.columns if 'geom' in c.lower()), None)
            if geom_col is None:
                print(f'  Could not identify geometry column in: {list(result.columns)}')
            else:
                fields_gdf = gpd.GeoDataFrame(
                    result,
                    geometry=gpd.GeoSeries.from_wkb(result[geom_col]),
                    crs='EPSG:4326'
                )
    except Exception as e:
        print(f'FoTW query failed: {e}')
        print('Dataset page: https://source.coop/ftw/global-data')

    if fields_gdf is not None and len(fields_gdf) > 0:
        fields_gdf = fields_gdf.clip(box(xmin, ymin, xmax, ymax))
        print(f'Fields clipped to zone: {len(fields_gdf)}')

        fig, ax = plt.subplots(figsize=(10, 8))
        fields_gdf.boundary.plot(ax=ax, linewidth=0.5, color='#2ca02c', alpha=0.7, label=f'FoTW fields ({len(fields_gdf)})')

        # Overlay flooded vs unflooded pixels from best event
        best_events = zone_df[zone_df['status'] == 'SUCCESS'].sort_values('percentage_flooded', ascending=False)
        if len(best_events) > 0:
            best_ev = best_events.iloc[0]
            ev_flooded = flooded_df[flooded_df['flood_event_id'] == best_ev['flood_event_id']]
            merged = pix_df.merge(ev_flooded[['pixel_id', 'flooded']], on='pixel_id', how='left')
            merged['flooded'] = merged['flooded'].fillna(False)
            unfl = merged[~merged['flooded']]
            fl   = merged[merged['flooded']]
            ax.scatter(unfl['lon'], unfl['lat'], s=3, c='#d73027', alpha=0.6, label='Unflooded agri')
            ax.scatter(fl['lon'],   fl['lat'],   s=3, c='#4575b4', alpha=0.9, label='Flooded agri')
            scene = str(best_ev['scene_date']) if pd.notnull(best_ev.get('scene_date')) else '—'
            ax.set_title(
                f"FoTW field boundaries vs flood pixels\n"
                f"Best event: {best_ev['flood_start'].strftime('%b %Y')} | Scene: {scene}",
                fontsize=10
            )
        else:
            ax.scatter(pix_df['lon'], pix_df['lat'], s=3, c='#999999', alpha=0.5, label='Agri pixels')
            ax.set_title('FoTW field boundaries — zone 000099_initial', fontsize=10)

        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.tick_params(labelsize=7)
        ax.legend(loc='upper right', fontsize=7, markerscale=3)
        ax.set_xlabel('Longitude'); ax.set_ylabel('Latitude')
        plt.tight_layout()
        plt.show()

    con.close()


# ---------------------------------------------------------------------------
# Standalone smoke-test
# ---------------------------------------------------------------------------

def main():
    conn = get_conn()

    print('=== Phase One Status ===')
    print(load_status(conn).to_string(index=False))
    print()

    print('=== Zone 000099_initial ===')
    zone_df, pix_df, flooded_df = load_zone_data(conn, '000099_initial')
    print(f'Events: {len(zone_df)}  Pixels: {len(pix_df):,}  '
          f'Flood labels: {len(flooded_df):,}')
    print()
    print(zone_df[['zone_id', 'flood_start', 'max_category',
                   'status', 'percentage_flooded', 'scene_date']].to_string(index=False))
    print()

    print('=== Flood summary ===')
    print(event_flood_summary(zone_df, flooded_df).to_string(index=False))

    conn.close()


if __name__ == '__main__':
    main()
