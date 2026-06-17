# Task: Embeddings collection + ablation — `agents/collect_embeddings.py` + notebook variant

**Phase 2, Step 3 (final ablation)** of `instructions/phase_2_plan.md`. **Strongly gated.**

## Do NOT start until ALL of:
1. Step 1 `GATE: PASS`.
2. Terrain-only baseline is in and tracked (`notebooks/04_terrain_baseline.ipynb`).
3. Cost estimate signed off (`reports/phase_2_embeddings_cost.md`) and the chosen option recorded.

(Plan §3, §4, §8.6: embeddings are the costliest, least-validated lever — last, not first.)

## Purpose
Collect embeddings for each field-year per the chosen option (Annual Satellite Embedding
**or** Clay/Prithvi seasonal) and run an **ablation**: terrain-only vs terrain+embeddings,
through the same location-disjoint harness. **Keep embeddings only if they measurably raise
ranking AUC** over terrain-only (plan §2). If they don't, report that and drop them.

## What to build
1. `agents/collect_embeddings.py` — fetch 64-dim vectors per (field × year), store
   alongside `field_dataset` (new table `field_embeddings`, FK to field_dataset, jsonb or
   float[] column). `--test`, `--dry-run`, `--zone-pattern`; CLAUDE.md conventions; exit 0/1.
2. Add `EMBEDDING_FEATURES` to `agents/feature_config.py` and a notebook variant routed
   through `validation.py` + logged via `tracking.py`.

## Report
Terrain-only AUC vs terrain+embeddings AUC (location-disjoint), with the keep/drop decision
stated explicitly and honestly.

## Out of scope
Calibration (Step 4), the external Y% source. No inference before the gates above pass.
