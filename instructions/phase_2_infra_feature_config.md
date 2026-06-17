# Task: Pin the feature list in code — `agents/feature_config.py`

**Phase 2 infrastructure** (`phase_2_plan.md` §8.2). Small, no dependencies.

## Purpose
Define the modelling feature set **once, in a single versioned module** — not ad-hoc per
notebook cell. Phase 1 failed reproducibility because the notebook and the PDF used
different feature sets (`reports/phase_one_analysis.md`, "Reproducibility problem").

## Deliverable
A plain Python module exporting named feature groups so every model run imports the exact
same lists and records which it used:

```python
TERRAIN_FEATURES = ["elevation","slope","hand","twi","flow_acc",
                    "dist_to_channel_m","dist_to_river_m","strahler_order",
                    "curvature","landcover","soil_hsg"]
SEASONAL_FEATURES = ["ndvi_premonsoon","soil_moisture","precip_premonsoon"]
EMBEDDING_FEATURES = []          # filled in by the embeddings ablation brief
TERRAIN_ONLY   = TERRAIN_FEATURES
TERRAIN_SEASON = TERRAIN_FEATURES + SEASONAL_FEATURES

FEATURE_SETS = {"terrain_only": TERRAIN_ONLY, "terrain_season": TERRAIN_SEASON}

def feature_set_hash(names: list[str]) -> str: ...   # short stable hash for MLflow logging
```

- No DB, no GEE, no side effects — pure declarations + a hash helper.
- Keep feature names **identical** to the keys written into `field_dataset.features`
  (see `phase_2_step_2_field_collection.md`).
- `soil_moisture`/`precip_premonsoon` stay in `SEASONAL_FEATURES` until the Step 2
  variance gate proves them constant — do not pre-drop.

## Out of scope
Loading data, training, the variance check itself (that lives in collection).
