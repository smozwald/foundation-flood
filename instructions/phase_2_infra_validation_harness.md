# Task: Location-disjoint validation harness — `agents/validation.py`

**Phase 2 infrastructure** (`phase_2_plan.md` §5, §8.3). No data dependency (codes against
the `field_dataset` schema). **This is the single most important infra piece** — Phase 1's
headline 0.991 AUC was leakage from spatially overlapping folds
(`reports/phase_one_analysis.md`, "Why the 0.991 pixel AUC is a red flag").

## Purpose
One reusable, **location-disjoint-by-default** cross-validation harness that every Phase 2
model routes through. Write the split **once**; no model rolls its own.

## Deliverable
```python
def leave_one_zone_out(df, group_col="zone_id"): ...
    # yields (train_idx, test_idx) with NO zone shared across the boundary

def spatial_block_cv(df, group_col="zone_id", n_splits=...): ...
    # grouped K-fold on zone for when zones are few

def evaluate_ranking(model_factory, df, features, label_col, splitter, budget_k_fn): ...
    # fits per fold, returns AUC (ranking: does it flood first?) + the metrics in §2 of the plan
```

## Hard requirements
- **Location-disjoint by default.** No field/pixel location may appear in both train and
  test of a fold. Grouping key = `zone_id`.
- **LOEO is allowed only as a labelled, known-optimistic upper bound** — never the headline,
  and the function name/return must mark it as such. Default path = leave-one-zone-out.
- **Budget K** (how many fields flood at a given Y%) comes from **train/external priors,
  never held-out truth** (§5). Accept a `budget_k_fn` computed on train only.
- Return the plan's §2 metrics so models can be checked against the contract: ranking AUC
  (target ≥0.65), and a hook to compare calibrated field-% RMSE vs **uniform spread of Y%**.
- Pure functions, deterministic seed, no DB writes. Print fold composition (which zones in
  test) so disjointness is auditable.

## Out of scope
The models themselves, MLflow logging (the model brief wires logging around this), calibration.
