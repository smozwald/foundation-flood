"""
MLflow tracking wrapper for Phase 2 model runs.

Provides a thin, consistent interface for logging model training runs.
Every model result must come from a tracked run containing:
  - Feature set name + hash (from feature_config.py)
  - Validation split name (from validation.py)
  - Model parameters
  - Phase 2 success metrics (ranking AUC, RMSE, stability, ablation AUC)

All runs are stored locally in mlruns/ (file-backed MLflow store, no remote server).
Logging only — this module does not train, load data, or run models.

Requires: mlflow (to be pinned and installed via notebook cell)
"""

import mlflow
from typing import dict, list, Any


def start_run(name: str, params: dict) -> None:
    """
    Open an MLflow run and log initial parameters.

    Args:
        name: Descriptive name for the run (e.g., "terrain_only_fold_0")
        params: Dictionary of model hyperparameters to log
               (e.g., {"max_depth": 5, "learning_rate": 0.01})

    Returns:
        None (opens an active MLflow run; caller must call mlflow.end_run() to close)
    """
    mlflow.start_run(run_name=name)
    for key, value in params.items():
        mlflow.log_param(key, value)


def log_feature_set(names: list[str]) -> None:
    """
    Log the feature set used in the model, including its stable hash.

    Imports feature_set_hash from feature_config to ensure reproducibility.

    Args:
        names: List of feature names used (e.g., ["elevation", "slope", "ndvi_premonsoon"])

    Returns:
        None
    """
    from agents.feature_config import feature_set_hash

    hash_val = feature_set_hash(names)
    mlflow.log_param("feature_set_names", ",".join(sorted(names)))
    mlflow.log_param("feature_set_hash", hash_val)


def log_split(splitter_name: str, fold_zones: list[str]) -> None:
    """
    Log the validation split strategy and test zone composition.

    Args:
        splitter_name: Name of the split strategy
                      (e.g., "leave_one_zone_out", "spatial_block_cv_fold_1")
        fold_zones: List of zone IDs held out in the test fold
                   (e.g., ["zone_A", "zone_B"])

    Returns:
        None
    """
    mlflow.log_param("split_strategy", splitter_name)
    mlflow.log_param("test_zones", ",".join(fold_zones))


def log_metrics(metrics: dict) -> None:
    """
    Log Phase 2 validation metrics for a completed fold.

    Expected metric keys (from phase_2_plan.md §2):
      - ranking_auc: Field-ranking AUC (target ≥0.65); must beat random (0.50)
      - calibrated_rmse: Calibrated field-% error under location-disjoint split
      - uniform_rmse: RMSE of uniform spread (null baseline)
      - spearman_rho: Rank stability (Cat-1 vs Cat-5); target ≥0.6
      - terrain_auc: Terrain-only model AUC (for embeddings ablation)
      - embeddings_auc: Terrain + embeddings model AUC (optional)

    Args:
        metrics: Dictionary of computed metrics with float values

    Returns:
        None
    """
    for key, value in metrics.items():
        mlflow.log_metric(key, value)


def end_run() -> None:
    """
    Close the active MLflow run.

    Call this after logging all parameters and metrics for a fold.

    Returns:
        None
    """
    mlflow.end_run()
