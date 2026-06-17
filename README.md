# foundation-flood

A portfolio project using satellite earth observation and foundation-model embeddings to rank agricultural flood vulnerability at field level, for rivers in Pakistan (Chenab and Indus basins).

# Introduction
Flood risk poses a great economic threat to agricultural productivity and food security. Climate change is increasing this risk, with previously rare floods occurring more often. While mitigating climate change is the best long-term defence, satellite-based flood-risk products can help farmers and other stakeholders prepare for likely flood damage.
This project asks whether foundation-model embeddings improve *pre-season* flood-risk assessment. We use Sentinel-1 / Sentinel-2 imagery (2015 onwards), with cloud-based processing and storage, and Claude AI agents to collect data across many rivers.

The Dartmouth Flood Observatory (DFO) provides discharge time series and return-period thresholds for rivers worldwide.
<img width="3600" height="1200" alt="image" src="https://github.com/user-attachments/assets/6bd87ce1-8417-4a68-8f72-8dc1276b2d83" />

The pipeline first collects flood dates and discharge values from the DFO and stores them in a Supabase PostGIS database (schema below).
<img width="1024" height="559" alt="image" src="https://github.com/user-attachments/assets/771b16e6-f4c5-40ed-a594-91e57006f49f" />

It then collects river geomorphology, terrain metrics, and Sentinel SAR imagery representing a dry-season baseline and the flood extent around each event. Historical per-pixel flood labels are derived by Otsu thresholding on Sentinel-1 backscatter.

**Architecture (revised after Phase 1).** Phase 1 showed that predicting *how much* area floods from local features alone does not work (field-level magnitude R² ≈ −0.27). Phase 2 therefore decouples magnitude from spatial pattern: the user picks a return period, an **external** hazard product supplies the expected flooded area (Y%), and **our** model disaggregates that Y% across fields by local vulnerability rank — using terrain features (TWI, HAND, distance to river) and AlphaEarth / Clay / Prithvi embeddings from the year preceding the season — calibrated so the field-average flooded area equals Y%. The output is a ranked, actionable list of which fields flood first as water rises. See `instructions/phase_2_plan.md`.

Data and models are tracked with MLflow as part of building a credible portfolio piece.

# Methodology
A hazard-downscaling approach: an external pre-season hazard product sets the flooded-area magnitude, while spatiotemporal foundation-model embeddings and terrain features rank relative field vulnerability within that magnitude.

# Plan

## Phase 1 - Notebook 01: Database Setup & GEE Ground Truth
- [x] Set up cloud environment on Google Earth Engine (GEE) and BigQuery
- [x] **Database Infrastructure:** Deploy PostGIS-enabled Supabase instance and define schemas for longitudinal pixel data
- [x] **River Data Collection:** Utilise the Dartmouth Flood Observatory to collect our rivers. Discrete Claud Agent. Initially ingested discharge time series, flood events, and threshold values for all 335 DFO stations globally. Subsequently trimmed to 13 Pakistan-focused stations (Chenab, Indus, Jhelum, Ravi, and Indus Delta) to stay within Supabase free-tier storage limits, removing ~1.3M `discharge_ts` rows, 12K `flood_events`, and 322 stations that had no downstream pixel data.
- [ ] **GEE SAR Pipeline:** Extract and process Sentinel-1 GRD (2015–2024) on-the-fly (Speckle filtering, Terrain Correction). Disrete Claude Agent.
- [ ] **Water Masking:** Execute Otsu thresholding on GEE to generate binary inundation masks. Discrete Claude Agent.
- [ ] **Data Ingestion:** Stream GEE results and Topographic metrics (HAND, TWI) into Supabase static and history tables

## Phase 2 - Notebook 04: Field-Level Disaggregation Model
The user picks a return period; an *external* hazard model supplies the expected flooded area (Y%); our model disaggregates that Y% across fields by local vulnerability rank, calibrated so the field-average equals Y%. Output: a ranked list of which fields flood first as water rises. Full plan: `instructions/phase_2_plan.md`.

- [ ] **External magnitude source:** Evaluate free pre-season hazard products (JRC/Copernicus, Google Flood Hub/GRRR) for Y% area flooded
- [ ] **Switch to fields:** Download FoTW field polygons and aggregate pixel-level labels and features to field level
- [ ] **Feature collection (field level):** ground truth, field static metrics, ndvi_premonsoon — zero-variance check before dropping any feature
- [ ] **Baseline-first modelling:** terrain-only model first; **foundation embeddings (AlphaEarth / Prithvi-EO-2.0 / Clay via `rs-embed`) added last as an ablation**, kept only if they beat terrain-only
- [ ] **Calibration layer:** distribute external Y% across fields by vulnerability rank, constrained so field-average = Y%
- [ ] **Experiment tracking:** MLflow from the first run (params, feature set, metrics)
- [ ] **Ranking validation:** confirm field rank generalises across zones (location-disjoint split) and beats a uniform-spread baseline

## Phase 3 - Scriptification & System Engineering
- [ ] **Modularization:** Refactor notebook logic into a clean Python package structure (`/src`)
- [ ] **ETL Automation:** Formalize the GEE-to-Supabase pipeline as a CLI-driven script for reproducibility
- [ ] **MLOps Pipeline:** Finalize training and logging workflows using MLflow and Vertex AI
- [ ] **Validation:** Run the script-base against unseen winter cycles to confirm performance stability

## Phase 4 - Agentic Scaling & Deployment
- [ ] **Agent Development:** Build an LLM-based agent capable of calling repository scripts to analyze new regions
- [ ] **Multi-Basin Execution:** Enable agent to delineate new rivers, trigger GEE processing, and populate new Supabase datasets
- [ ] **Decision Support:** Create a dashboard to visualize field-level vulnerability under various simulated rainfall scenarios
