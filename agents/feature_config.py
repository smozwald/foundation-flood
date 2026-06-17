"""
Phase 2 feature configuration module.

Pure Python declarations of modelling feature groups for reproducibility.
No DB, no GEE, no side effects.

Feature names match keys written into field_dataset.features (phase_2_step_2_field_collection.md).
"""

import hashlib


TERRAIN_FEATURES = [
    "elevation",
    "slope",
    "hand",
    "twi",
    "flow_acc",
    "dist_to_channel_m",
    "dist_to_river_m",
    "strahler_order",
    "curvature",
    "landcover",
    "soil_hsg",
]

SEASONAL_FEATURES = [
    "ndvi_premonsoon",
    "soil_moisture",
    "precip_premonsoon",
]

# Embeddings filled in by the embeddings ablation brief
EMBEDDING_FEATURES = []

TERRAIN_ONLY = TERRAIN_FEATURES
TERRAIN_SEASON = TERRAIN_FEATURES + SEASONAL_FEATURES

FEATURE_SETS = {
    "terrain_only": TERRAIN_ONLY,
    "terrain_season": TERRAIN_SEASON,
}


def feature_set_hash(names: list[str]) -> str:
    """
    Compute a short stable hash for a feature set.

    Used for MLflow logging to uniquely identify feature configurations.

    Args:
        names: list of feature names

    Returns:
        8-character hex hash of the sorted, newline-joined feature names
    """
    # Sort for stability across runs
    sorted_names = "\n".join(sorted(names))
    hash_obj = hashlib.sha256(sorted_names.encode("utf-8"))
    return hash_obj.hexdigest()[:8]
