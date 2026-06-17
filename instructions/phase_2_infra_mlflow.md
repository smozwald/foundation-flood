# Task: MLflow tracking wrapper — `agents/tracking.py`

**Phase 2 infrastructure** (`phase_2_plan.md` §6, §8.1). Small, no dependencies.

## Purpose
Every Phase 2 model number must come from a **tracked run** (params + feature set + metric),
logged from the first model — never retrofitted from a PDF. Phase 1's numbers existed only
in a PDF that the committed notebook could not reproduce
(`reports/phase_one_analysis.md`, "Reproducibility problem").

## Deliverable
A thin MLflow wrapper so model code logs consistently:
```python
def start_run(name, params: dict): ...           # opens an MLflow run, logs params
def log_feature_set(names: list[str]): ...        # logs the exact feature list + its hash
def log_metrics(metrics: dict): ...
def log_split(splitter_name, fold_zones): ...      # records the validation split used
```
- Local file-backed MLflow store (e.g. `mlruns/`); no remote server required.
- Pin `mlflow` in a requirements note; include the install in the modelling notebook cell
  (do not run pip in shell — project convention).
- Logging only — no training, no data loading.

## Required logged fields (the reproducibility contract)
Each run must record: feature-set name + hash (from `feature_config.py`), validation split
name (from `validation.py`), model params, and the §2 metrics. A run missing any of these is
not a valid Phase 2 result.

## Out of scope
The models, the validation split logic, the feature definitions (imported, not redefined).
