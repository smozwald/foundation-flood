# Task: Calibration layer — `agents/calibrate.py` + notebook section

**Phase 2, Step 4** of `instructions/phase_2_plan.md`. The layer that turns relative ranks
into an actionable, magnitude-anchored output.

## Depends on
- Step 1: `external_flood_hazard` (Y% per return period per zone).
- Step 3: the winning ranking model (terrain-only unless the ablation kept embeddings).

## Purpose
Map per-field vulnerability **ranks → field-level flood %**, constrained so the
**field-average equals the external Y%** for the chosen return period. Magnitude stays
external; the model only orders fields; calibration distributes Y% along that order.

## Method
1. Pick a return period → look up `Y%` from `external_flood_hazard`.
2. Order fields by model rank.
3. Distribute Y% across fields by rank such that `mean(field_pct) == Y%` (constraint), e.g.
   a monotone mapping from rank percentile to field-% calibrated to integrate to Y%.
4. Output: ranked, actionable list — which fields flood first as water rises.

## The acceptance test (plan §2 — the real bar)
Compare calibrated field-% against held-out truth, **location-disjoint**, vs the null
architecture **uniform spread of Y%** (every field = Y%):
- Calibrated RMSE must be **lower than uniform spread**.
- **If it is not, the honest conclusion is "external Y% + uniform spread is the operational
  recommendation"** — a legitimate portfolio result. Report it, do not hide it.
Also report rank stability (Cat-1 vs Cat-5, ρ ≥ 0.6).

> Note: the location-disjoint validation of "ranking beats uniform spread" is the top
> **deferred open risk** (plan §7). This brief implements the calibration + the test; whether
> the test is run now or on revisit follows the plan's deferral decision — wire it so it is
> ready to run on a location-disjoint split.

## Conventions
CLAUDE.md: `.env` conn, `%(name)s`, stdout, exit 0/1, `--test`/`--dry-run`. Budget K from
train/external priors, never held-out truth (§5).

## Out of scope
The external source, the ranking model internals, embeddings.
