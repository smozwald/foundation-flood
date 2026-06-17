# Task: Terrain-only baseline ranking model — `notebooks/04_terrain_baseline.ipynb`

**Phase 2, Step 3** of `instructions/phase_2_plan.md`. **Baseline first** — establishes
whether cheap terrain features clear the success bar before any embedding spend.

## Depends on
`field_dataset` populated (Step 2), `agents/feature_config.py`, `agents/validation.py`,
`agents/tracking.py`. Import these — do **not** redefine features, splits, or logging inline.

## Purpose
Train a **field vulnerability ranking** model (which fields flood first as water rises) on
**terrain-only** features and evaluate it against the plan's §2 success contract. The model
outputs a **relative, dimensionless rank** — never an absolute %. Magnitude is external.

## What to build (notebook, multiple variants, all MLflow-tracked)
1. Load `field_dataset` (apply the pixel-support threshold from Step 2).
2. Variant A: `terrain_only` (the headline baseline).
3. Variant B: `terrain_season` (adds `ndvi_premonsoon` + any features the variance gate kept)
   — quantifies what seasonal features add over terrain alone.
4. Route **every** variant through `validation.leave_one_zone_out` (location-disjoint).
   Report LOEO only as a labelled optimistic upper bound, never as headline.
5. Log every run via `tracking.py`: feature-set name+hash, split, params, metrics.

## Success contract to report (plan §2)
| Metric | Target |
|---|---|
| Field-ranking AUC (floods first?), location-disjoint | ≥ 0.65 vs random 0.50 |
| Rank stability Cat-1 vs Cat-5 | Spearman ρ ≥ 0.6 |
| Terrain-only vs +seasonal | does adding features measurably raise AUC? |

State plainly whether the baseline clears 0.65 location-disjoint. **A negative result is a
legitimate portfolio outcome** (plan §2) — report it honestly, do not tune to the test set.

## Guardrails
- Budget K from train/external priors, never held-out truth (§5; use the harness hook).
- No discharge/rainfall as inputs (magnitude is external).
- Notebook cells must actually execute with outputs committed (Phase 1's did not).
- Include pip installs in a notebook cell (project convention; no shell pip).

## Out of scope
Embeddings (next brief, gated on this baseline), calibration (Step 4), the external Y% source.
