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

## Phase 1 - Notebook 01: Database Setup & GEE Ground Truth
- [x] Set up cloud environment on Google Earth Engine (GEE) and BigQuery
- [x] **Database Infrastructure:** Deploy PostGIS-enabled Supabase instance and define schemas for longitudinal pixel data
- [ ] **River Delineation:** Programmatically define the Po River corridor (5km buffer) as the primary AOI
- [ ] **GEE SAR Pipeline:** Extract and process Sentinel-1 GRD (2015–2024) on-the-fly (Speckle filtering, Terrain Correction)
- [ ] **Water Masking:** Execute Otsu thresholding on GEE to generate binary inundation masks
- [ ] **Data Ingestion:** Stream GEE results and Topographic metrics (HAND, TWI) into Supabase static and history tables

## Phase 2 - Notebook 02: Embedding Analysis & MLflow
- [ ] **Foundation Benchmarking:** Use `rs-embed` to extract and compare Prithvi-EO-2.0 and Clay embeddings for pre-season windows
- [ ] **Meteorological Join:** Integrate precipitation and discharge data with Supabase records via SQL views
- [ ] **Experiment Tracking:** Configure MLflow to track model architectures, hyperparameters, and embedding versions
- [ ] **Hypothesis Testing:** Compare baseline rainfall models against embedding-enriched models to validate the "Soil Memory" effect

## Phase 3 - Scriptification & System Engineering
- [ ] **Modularization:** Refactor notebook logic into a clean Python package structure (`/src`)
- [ ] **ETL Automation:** Formalize the GEE-to-Supabase pipeline as a CLI-driven script for reproducibility
- [ ] **MLOps Pipeline:** Finalize training and logging workflows using MLflow and Vertex AI
- [ ] **Validation:** Run the script-base against unseen winter cycles to confirm performance stability

## Phase 4 - Agentic Scaling & Deployment
- [ ] **Agent Development:** Build an LLM-based agent capable of calling repository scripts to analyze new regions
- [ ] **Multi-Basin Execution:** Enable agent to delineate new rivers, trigger GEE processing, and populate new Supabase datasets
- [ ] **Decision Support:** Create a dashboard to visualize field-level vulnerability under various simulated rainfall scenarios
