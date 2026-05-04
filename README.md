# foundation-flood

This portfolio piece aims to utilize foundation vision models to improve forecasting of crop yield failure, focusing on the relationship between soil memory and inundation in the Skåne region, Sweden.

# Introduction
Flood risk poses a great economic threat to agricultural productivity and secure food systems. Climate change is enhancing this risk, with ever-increasing occurence of previously low incidence floods. Whilst reducing the impacts of climate change is the best long-term option to mitigate flood damage, satellite-based flood risk products can help farmers and other stakeholders better prepare for possible flood damage.
The purpose of this project is thus to assess how foundation model embeddings may improve pre-season flood risk. We will focus on the sentinel-1/sentinel-2 datasets available from 2015 onwards, with processing and data storage performed in the cloud. Utilising AI agents, we will collect data for many rivers using Claude AI agents.

The DFO-FLood observatory provides discharge values for many rivers across the world, also providing information on threshold values.
<img width="3600" height="1200" alt="image" src="https://github.com/user-attachments/assets/6bd87ce1-8417-4a68-8f72-8dc1276b2d83" />

Our modelling will involve first collecting flood dates and values using the DFO-Observatroy for many rivers and storing them in a supabase postGIS database. Database plan below
<img width="1024" height="559" alt="image" src="https://github.com/user-attachments/assets/771b16e6-f4c5-40ed-a594-91e57006f49f" />

Following this we will collect information on river geomorphology, rainfall, and satellite images representing both a baseline and flood extent in the week before and weeks following a flood.
The below section on modelling is subject to being changed at a later point. The plan is to incorporate 
New advances in machine learning allow for further iteration on flood models. In the context of agricultural risk, floods are an important natural hazard to account for. What this project investigates is the potential to assess risk for agricultural fields under various rainfall scenarios, utilizing typical data sources and features in flood modelling (Topographic Water Index, distance to river, precipitation) as well as Google AlphaEarth Embeddings for the year preceding the agricultural season. For the predictive y-variable (flooded area), we estimate this for past years using the Privthi water mapping model, which achieves high accuracy at detecting flooded pixels utilizing Sentinel-2 data.
The ultimate goal is to create a model utilizing as a base input, the AlphaEarth embeddings + additional data, and then provide continuous rainfall on a per pixel basis, predicting when a pixel may become flooded.

Data and Models are to be logged using MLOps, as part of creating a good portfolio piece.


# Methodology
Methodology of a physics-informed solution using spatiotemporal embeddings to isolate pre-season legacy risk from in-season rainfall spikes.

# Plan

## Phase 1 - Notebook 01: Database Setup & GEE Ground Truth
- [x] Set up cloud environment on Google Earth Engine (GEE) and BigQuery
- [x] **Database Infrastructure:** Deploy PostGIS-enabled Supabase instance and define schemas for longitudinal pixel data
- [ ] **River Delineation:** Utilise the Dartmouth Flood Observatory to collect our rivers. Discrete Claud Agent.
- [ ] **GEE SAR Pipeline:** Extract and process Sentinel-1 GRD (2015–2024) on-the-fly (Speckle filtering, Terrain Correction). Disrete Claude Agent.
- [ ] **Water Masking:** Execute Otsu thresholding on GEE to generate binary inundation masks. Discrete Claude Agent.
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
