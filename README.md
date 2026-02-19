# foundation-flood

This portfolio piece aims to utilize foundation vision models to improve forecasting of crop yield failure, focusing on the relationship between soil memory and inundation in the Skåne region, Sweden.

# Introduction
Privthi model is used to track water coverage in flood events, with high accuracy using Sentinel-2. The Skane region of Sweden is interesting as there is a high incidence of floods which appear in the years of Sentinel-2 coverage, with higher recurrence at higher latitudes.
[Flood Risk Assessment for the Kävlinge River for Present and Future Climate Scenarios using HEC-RAS Rain-on-Grid (Roosli, 2024)](https://lup.lub.lu.se/student-papers/record/9163737) overviews past floods in the Kavlinge river, a basin with several past high-discharge events.

Data for this region can be found for discharge, precipitation and other things at (https://www.smhi.se/data).
For this project, we use precipitation from Horby, and discharge from Hogsmolla station.

River discharge is strongly linked not just to immediate rainfall effects, but to environmental conditions and the buildup of moisture through a season.
The below graph shows the correlation at the aforementioned sites between rainfall and discharge when utilizing immediate preceding monthly rainfall, and 6 month aggregations.
<img width="1600" height="600" alt="image" src="https://github.com/user-attachments/assets/d886a9f5-4961-4cda-8802-efaadd160236" />

This is well-considered in flood modelling and prediction.
New advances in machine learning allow for further iteration on flood models. In the context of agricultural risk, floods are an important natural hazard to account for. What this project investigates is the potential to assess risk for agricultural fields under various rainfall scenarios, utilizing typical data sources and features in flood modelling (Topographic Water Index, distance to river, precipitation) as well as Google AlphaEarth Embeddings for the year preceding the agricultural season. For the predictive y-variable (flooded area), we estimate this for past years using the Privthi water mapping model, which achieves high accuracy at detecting flooded pixels utilizing Sentinel-2 data.
The ultimate goal is to create a model utilizing as a base input, the AlphaEarth embeddings + additional data, and then provide continuous rainfall on a per pixel basis, predicting when a pixel may become flooded.

Data and Models are to be logged using MLOps, as part of creating a good portfolio piece.


# Methodology
Methodology of a physics-informed solution using spatiotemporal embeddings to isolate pre-season legacy risk from in-season rainfall spikes.

# Plan

## Phase 1 - Base Data
- [ ] Outline project plan
- [x] Set up cloud environment on Google Earth Engine and BigQuery
- [ ] Literature review of winter wheat and rapeseed flood tolerance
- [ ] Collect Jordbruksverket LPIS field boundaries (Jordbruksblock) for Skåne
- [ ] Filter field polygons to those intersecting high-risk river buffers (Kävlingeån)
- [ ] Identify peak rainfall spikes and discharge events for each winter season (2015-2024)
- [ ] Create and ingest historical meteorological data and field geometries into BigQuery tables

## Phase 2 -- Collect and collate all data
- [ ] Notebook to test extracting target fields using Jordbruksverket LPIS data
- [ ] Create pipeline to extract representative points per field (Centroid and Lowest Elevation Point)
- [ ] Extract AlphaEarth Satellite Embeddings for representative points during the November legacy window
- [ ] Use Prithvi-EO-2.0 and Sentinel-1 SAR to generate field-level flood masks (Jan-Mar)
- [ ] Calculate daily inundation reduction rates for fields to identify drainage stagnation
- [ ] Update BigQuery Tables to include embeddings and topographic wetness indices
- [ ] Join field-level saturation DNA with historical flood outcomes

## Phase 3
- [ ] Set up model tracking backend using MLflow
- [ ] Initial modeling in notebooks: Compare in-season rainfall models against models enriched with pre-season embeddings
- [ ] Assessment of model viability for long-term forecasting using twin-year scenarios (e.g., 2022 vs 2024)
- [ ] Assessment of best data preparation and topographic feature weighting
- [ ] Test data on unseen winter cycles to validate predictive power of soil memory

## Phase 4
- [ ] Track models online using MLOps (Vertex AI and MLflow)
- [ ] Simulation of addition of new SAR and meteo data for seasonal risk serving
- [ ] Serve model predictions via a decision support interface for field-level vulnerability
