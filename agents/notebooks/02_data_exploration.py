# ============================================================
# CELL 1 — Install dependencies
# ============================================================

# %%
!pip install psycopg2-binary python-dotenv reverse_geocoder pycountry-convert pandas matplotlib


# ============================================================
# CELL 2 — Imports, DB connection, load stations with country/continent
# ============================================================

# %%
import os
import psycopg2
import pandas as pd
import reverse_geocoder as rg
import pycountry_convert as pc
from dotenv import load_dotenv

load_dotenv()
CONN_STRING = os.environ["SUPABASE_CONN_STRING"]

# Load all stations with lat/lon from PostGIS geometry
with psycopg2.connect(CONN_STRING) as conn:
    stations_df = pd.read_sql("""
        SELECT
            station_id,
            dfo_station_id,
            station_name,
            ST_Y(geom::geometry) AS lat,
            ST_X(geom::geometry) AS lon
        FROM discharge_stations
    """, conn)

# Derive country and continent from coordinates
coords = list(zip(stations_df["lat"], stations_df["lon"]))
geo_results = rg.search(coords, verbose=False)
stations_df["country_code"] = [r["cc"] for r in geo_results]
stations_df["country"]      = [r["name"].split(",")[-1].strip() for r in geo_results]

# Use country name from pycountry_convert for consistency
def cc_to_country(cc):
    try:
        return pc.country_alpha2_to_country_name(cc)
    except Exception:
        return cc

def cc_to_continent(cc):
    try:
        cont_code = pc.country_alpha2_to_continent_code(cc)
        return pc.convert_continent_code_to_continent_name(cont_code)
    except Exception:
        return "Unknown"

stations_df["country"]   = stations_df["country_code"].apply(cc_to_country)
stations_df["continent"] = stations_df["country_code"].apply(cc_to_continent)

print(f"Loaded {len(stations_df)} stations across "
      f"{stations_df['country'].nunique()} countries, "
      f"{stations_df['continent'].nunique()} continents")
print(stations_df[["station_name", "country", "continent"]].head())


# ============================================================
# CELL 3 — Load flood event counts per station
# ============================================================

# %%
with psycopg2.connect(CONN_STRING) as conn:
    events_df = pd.read_sql("""
        SELECT
            station_id,
            max_category,
            COUNT(*) AS event_count
        FROM flood_events
        WHERE detection_method = 'threshold_exceedance_7d'
        GROUP BY station_id, max_category
    """, conn)

# Total events per station
total_events = (
    events_df.groupby("station_id")["event_count"]
    .sum()
    .rename("total_events")
    .reset_index()
)

# Events per category per station (pivoted)
events_pivot = (
    events_df.pivot_table(
        index="station_id", columns="max_category",
        values="event_count", aggfunc="sum", fill_value=0
    )
    .rename(columns={c: f"cat{c}_events" for c in range(1, 6)})
    .reset_index()
)

stations_full = (
    stations_df
    .merge(total_events,  on="station_id", how="left")
    .merge(events_pivot,  on="station_id", how="left")
    .fillna(0)
)
for col in ["total_events"] + [f"cat{c}_events" for c in range(1, 6)]:
    stations_full[col] = stations_full[col].astype(int)

print(stations_full[["station_name", "country", "continent", "total_events"]].head())


# ============================================================
# CELL 4 — Summary per Country
# ============================================================

# %%
cat_cols = [f"cat{c}_events" for c in range(1, 6)]

country_summary = (
    stations_full
    .groupby("country")
    .agg(
        total_rivers        = ("station_id",    "count"),
        total_flood_events  = ("total_events",  "sum"),
        rivers_gt15_events  = ("total_events",  lambda x: (x > 15).sum()),
        **{col: (col, "sum") for col in cat_cols},
    )
    .reset_index()
)
country_summary["pct_rivers_gt15_events"] = (
    (country_summary["rivers_gt15_events"] / country_summary["total_rivers"] * 100)
    .round(1)
)
country_summary = country_summary.sort_values("total_rivers", ascending=False)

print("=== Per Country ===")
display_cols = ["country", "total_rivers", "total_flood_events"] + cat_cols + ["pct_rivers_gt15_events"]
print(country_summary[display_cols].to_string(index=False))


# ============================================================
# CELL 5 — Summary per Continent
# ============================================================

# %%
continent_summary = (
    stations_full
    .groupby("continent")
    .agg(
        total_rivers        = ("station_id",    "count"),
        total_flood_events  = ("total_events",  "sum"),
        rivers_gt15_events  = ("total_events",  lambda x: (x > 15).sum()),
        **{col: (col, "sum") for col in cat_cols},
    )
    .reset_index()
)
continent_summary["pct_rivers_gt15_events"] = (
    (continent_summary["rivers_gt15_events"] / continent_summary["total_rivers"] * 100)
    .round(1)
)
continent_summary = continent_summary.sort_values("total_rivers", ascending=False)

print("=== Per Continent ===")
display_cols = ["continent", "total_rivers", "total_flood_events"] + cat_cols + ["pct_rivers_gt15_events"]
print(continent_summary[display_cols].to_string(index=False))


# ============================================================
# CELL 6 — Discharge + flood event visualisation
# ============================================================

# %%
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
import matplotlib.dates as mdates

# ----------------------------------------------------------
# USER INPUT — set REGION_TYPE to 'continent' or 'country'
# ----------------------------------------------------------
REGION_TYPE = "continent"   # 'continent' or 'country'
REGION_NAME = "Asia"        # e.g. 'Asia', 'South America', 'Australia', 'India'
# ----------------------------------------------------------

assert REGION_TYPE in ("continent", "country"), "REGION_TYPE must be 'continent' or 'country'"

region_stations = stations_full[stations_full[REGION_TYPE] == REGION_NAME].copy()
if region_stations.empty:
    raise ValueError(f"No stations found for {REGION_TYPE}='{REGION_NAME}'. "
                     f"Available: {sorted(stations_full[REGION_TYPE].unique())}")

# Select up to 2 stations with <=15 total events, then up to 2 with >15
low_event  = region_stations[region_stations["total_events"] <= 15].head(2)
high_event = region_stations[region_stations["total_events"] >  15].head(2)
selected   = pd.concat([low_event, high_event]).head(4)
station_ids = selected["station_id"].tolist()

print(f"Selected stations for {REGION_TYPE}='{REGION_NAME}':")
print(selected[["station_name", "country", "total_events"]].to_string(index=False))

# Load discharge time series (2015 onwards)
with psycopg2.connect(CONN_STRING) as conn:
    ts_df = pd.read_sql("""
        SELECT station_id, obs_date, discharge_m3s
        FROM discharge_ts
        WHERE station_id = ANY(%(ids)s::uuid[])
          AND obs_date >= '2015-01-01'
        ORDER BY station_id, obs_date
    """, conn, params={"ids": station_ids})

    # Load flood thresholds for selected stations
    thresh_df = pd.read_sql("""
        SELECT station_id, category, discharge_threshold_m3s
        FROM flood_thresholds
        WHERE station_id = ANY(%(ids)s::uuid[])
        ORDER BY station_id, category
    """, conn, params={"ids": station_ids})

    # Load flood events for selected stations (2015 onwards)
    fevents_df = pd.read_sql("""
        SELECT station_id, flood_start, flood_end, max_category
        FROM flood_events
        WHERE station_id = ANY(%(ids)s::uuid[])
          AND flood_start >= '2015-01-01'
          AND detection_method = 'threshold_exceedance_7d'
        ORDER BY station_id, flood_start
    """, conn, params={"ids": station_ids})

ts_df["obs_date"]         = pd.to_datetime(ts_df["obs_date"])
fevents_df["flood_start"] = pd.to_datetime(fevents_df["flood_start"])
fevents_df["flood_end"]   = pd.to_datetime(fevents_df["flood_end"])

# Colour maps
THRESHOLD_COLORS = {1: "#2196F3", 2: "#4CAF50", 3: "#FF9800", 4: "#F44336", 5: "#9C27B0"}
FLOOD_GREYS      = {1: "#D3D3D3", 2: "#A9A9A9", 3: "#808080", 4: "#555555", 5: "#2F2F2F"}
CAT_LABELS       = {
    1: "Cat 1 (2–5 yr)",
    2: "Cat 2 (5–10 yr)",
    3: "Cat 3 (10–20 yr)",
    4: "Cat 4 (20–50 yr)",
    5: "Cat 5 (50+ yr)",
}

n = len(station_ids)
fig, axes = plt.subplots(n, 1, figsize=(16, 4 * n), sharex=False)
if n == 1:
    axes = [axes]

for ax, sid in zip(axes, station_ids):
    row   = selected[selected["station_id"] == sid].iloc[0]
    ts    = ts_df[ts_df["station_id"] == sid].copy()
    evts  = fevents_df[fevents_df["station_id"] == sid].copy()
    thrsh = thresh_df[thresh_df["station_id"] == sid].copy()

    # Shade flood event periods
    for _, ev in evts.iterrows():
        ax.axvspan(ev["flood_start"], ev["flood_end"],
                   color=FLOOD_GREYS.get(ev["max_category"], "#808080"),
                   alpha=0.6, zorder=1)

    # Discharge line
    ax.plot(ts["obs_date"], ts["discharge_m3s"],
            color="steelblue", linewidth=0.8, zorder=2, label="Discharge (m³/s)")

    # Category threshold lines
    for _, tr in thrsh.iterrows():
        cat = int(tr["category"])
        ax.axhline(tr["discharge_threshold_m3s"],
                   color=THRESHOLD_COLORS.get(cat, "black"),
                   linewidth=1.2, linestyle="--", zorder=3)

    event_flag = f"({row['total_events']} events {'— HIGH' if row['total_events'] > 15 else ''})"
    ax.set_title(f"{row['station_name']}  |  {row['country']}  {event_flag}", fontsize=11)
    ax.set_ylabel("Discharge (m³/s)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.tick_params(axis="x", rotation=45)

# Shared legend
threshold_handles = [
    Line2D([0], [0], color=THRESHOLD_COLORS[c], linewidth=1.5,
           linestyle="--", label=CAT_LABELS[c])
    for c in range(1, 6)
]
flood_handles = [
    mpatches.Patch(color=FLOOD_GREYS[c], alpha=0.7, label=f"Flood {CAT_LABELS[c]}")
    for c in range(1, 6)
]
discharge_handle = Line2D([0], [0], color="steelblue", linewidth=1.5, label="Discharge (m³/s)")

fig.legend(
    handles=[discharge_handle] + threshold_handles + flood_handles,
    loc="lower center",
    ncol=4,
    fontsize=9,
    bbox_to_anchor=(0.5, -0.02),
    frameon=True,
)

fig.suptitle(
    f"Flood Discharge — {REGION_NAME}  (2015–2025)\n"
    f"Dashed lines = category thresholds | Grey shading = flood events",
    fontsize=13, y=1.01,
)
plt.tight_layout()
plt.show()
