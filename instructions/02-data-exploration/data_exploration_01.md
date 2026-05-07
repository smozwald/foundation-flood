# 02_database_exploration.ipynb
## Purpose
The purpose of this instruction is for you to output into agents/notebooks/02_data_exploration.py code which i can paste into respective cells of my data exploration notebook.
YOu will not do the analysis yourself, only confirm all database info can be gathered, and then you will write the scripts i can paste into notebook.

## Connection 
Load `SUPABASE_CONN_STRING` from `.env` via python-dotenv.
Use `with psycopg2.connect(CONN_STRING) as conn`.

## Extensions required
- `postgis`
- `vector`
- `uuid-ossp`

## Look at database yourself
First look at the database online yourself, you will find in the table 'discharge_stations' dfo_station_id, station_name, geom, and a generated station_id (uuid). 
station_id is a foreign key in the following tables of interest:
- flood_thresholds: Threshold required to register a flood in each river based on discharge_threshold_m3s (there are 5 categories, 1 being 2-5 year recuurence, 5 being 50+ year).
- flood_events: Registered flood events and start and end days for each station_id.
discharge_ts. 10-year daily discharge.
- discharge_ts: for each obs_date we show discahrge_m3s

## Cells to create. 
# Database Exploration

Having created our database by downloading and logging flood events with the Dartmouth Flood Observatory, we want to select the rivers to use, and explore some samples in Google Earth Engine. This must include the following steps in each cell

1. Connect to Supabase, provide summary of rivers. We will make a regional model, and so should look at dividing into major regions. We want to calculate the following data(First per Country, second per Continent in two seperate adjacent cells):
a. Total Rivers Per Country/Continent
c. Total Flood Events Per Country/Continent (also total flood events of each of the 5 categories per continent
d. Percentage of Rivers per Country/Continent with >15 total events. (These are likely erroneous).

2. As above, per continent.

3. Visualization of Rivers in user-defined continent/country (Text input at top of cell). Select up to 4 rivers, show flood discharge graph from 2015, with lines indicating cutoff on y-axis for flood categories. Ideally first 2 should have <15 total events, and second >15 total events so we can assess the issues. X-axis represents time, shaded grey in period of flood (for each event in flood_events, start_date and end_date indicate this). Shade of grey goes from light grey for category 1 to dark grey for category 5. Ensure legend.

## Other Info
Data extracted programmatically, not yet spot checked.
Data limited from Jan 1 2015 - Dec 31 2025. 
Null values may appear where data wasn't logged correctly or stations out of commission.
Null columns appear which will be finished in a future step.