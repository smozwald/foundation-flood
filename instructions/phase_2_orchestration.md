# Phase 2 — Orchestration Plan

**Date:** 2026-06-17
**Role:** Claude (this session) acts as orchestrator: assigns a model per brief, sequences by
dependency, launches agents, and monitors to completion.
**Source of truth for scope:** `instructions/phase_2_plan.md`. Each task below has its own brief.

---

## Task table

| ID | Brief | Deliverable | Model | Why this model |
|----|-------|-------------|-------|----------------|
| T1 | `phase_2_step_1_external_y.md` | `agents/fetch_external_y.py` (Y% gate) | **Sonnet** | GEE + DB, well-specified |
| T2 | `phase_2_step_2_field_collection.md` | `agents/collect_field_dataset.py` | **Sonnet** | GEE + DB aggregation |
| T3 | `phase_2_infra_feature_config.md` | `agents/feature_config.py` | **Haiku** | Pure declarations |
| T4 | `phase_2_infra_validation_harness.md` | `agents/validation.py` | **Opus** | Leakage-critical; burned Phase 1 |
| T5 | `phase_2_infra_mlflow.md` | `agents/tracking.py` | **Haiku** | Thin logging wrapper |
| T6 | `phase_2_embeddings_cost_estimate.md` | `reports/phase_2_embeddings_cost.md` | **Sonnet** | Analysis/doc, no compute |
| T7 | `phase_2_label_validation_ems.md` | `agents/validate_labels_ems.py` + report | **Sonnet** | Data wrangling + IoU |
| T8 | `phase_2_step_3_terrain_baseline.md` | `notebooks/04_terrain_baseline.ipynb` | **Opus** | Core modelling, contract-critical |
| T9 | `phase_2_step_3b_embeddings_ablation.md` | `agents/collect_embeddings.py` + variant | **Sonnet** | Collection + ablation run |
| T10 | `phase_2_step_4_calibration.md` | `agents/calibrate.py` + section | **Opus** | Constrained mapping, the real bar |

---

## Dependency graph (waves)

```
WAVE 0  (no deps — run in parallel now)
  T1  External Y% gate ........ Sonnet   ← GATE: must PASS to unlock Wave 1
  T3  Feature config .......... Haiku
  T4  Validation harness ...... Opus
  T5  MLflow wrapper .......... Haiku
  T6  Embeddings cost doc ..... Sonnet
  T7  EMS label validation .... Sonnet   (independent; informs label trust)

WAVE 1  (needs T1 = GATE: PASS)
  T2  Field collection ........ Sonnet

WAVE 2  (needs T2 + T3 + T4 + T5)
  T8  Terrain-only baseline ... Opus     ← reports §2 success contract

WAVE 3  (needs T8 in)
  T9  Embeddings ablation ..... Sonnet   (also needs T1 PASS + T6 signed off)
  T10 Calibration layer ....... Opus     (also needs T1 Y% values)
```

## Gate rules
- **T1 FAIL → halt.** Do not launch Wave 1. The honest conclusion becomes "external Y% +
  uniform spread"; record and stop the build.
- **T8 baseline below AUC 0.65 location-disjoint** is a *legitimate* result (plan §2) — it
  does not halt T10, which then tests ranking vs uniform spread (the real architecture bar).
- **T9 embeddings** only if T8 is in, T1 passed, and the cost doc (T6) is signed off.

## Monitoring approach
- Wave 0 agents launched with `run_in_background`; orchestrator is notified on each completion.
- On T1 completion, read its `GATE:` verdict before launching T2.
- Progress tracked in the session todo list (one item per task, plus per-wave gates).
- Each agent points at its brief + `phase_2_plan.md` + `CLAUDE.md`; reports back its
  deliverable path and any gate verdict.

---

## CURRENT STATUS — session handoff (2026-06-17)

> Read this first on resuming. Orchestrator = main session; execution tasks run as
> background subagents on their assigned model. **Permissions reload required:**
> `.claude/settings.local.json` was extended with `Bash(pip|nohup|grep|cat|tail|head|ls|sleep|mkdir|wc|find|echo *)`
> and `Read(//tmp/**)` so subagents can run unattended — this only takes effect in a
> session started *after* that edit. (That's why this is being resumed in a new session.)

### Wave 0 — COMPLETE ✅
| Task | File(s) | Notes |
|---|---|---|
| T1 ✅ | `agents/fetch_external_y.py`, table `external_flood_hazard` (91 rows) | **GATE: PASS.** Asset `JRC/CEMS_GLOFAS/FloodHazard/v1` is deprecated → used **v2_1** (bands `RP10_depth`…`RP500_depth`, ~92.77 m). Zone 000099 RP10=38.76% vs observed Cat-5 36.9% ✓. **Watch:** 7/13 zones saturate >75% at RP10 (no internal gradient to rank — affects zone selection for modelling). |
| T3 ✅ | `agents/feature_config.py` | Pinned feature sets + `feature_set_hash`. |
| T4 ✅ | `agents/validation.py` | Location-disjoint harness; `_assert_disjoint` guard; LOEO renamed `leave_one_event_out_OPTIMISTIC`; `uniform_spread_rmse`/`calibrated_rmse` hooks. **TODO:** needs sklearn — run `python agents/validation.py --self-test` once installed. |
| T5 ✅ | `agents/tracking.py` | MLflow wrapper, local `mlruns/`. |
| T6 ✅ | `reports/phase_2_embeddings_cost.md` | Option B (Clay/Prithvi seasonal) recommended *if* embeddings used. Gate: only if terrain-only fails/shows gap; drop if AUC lift < 0.02. |
| T7 ✅ | `agents/validate_labels_ems.py`, `reports/phase_2_label_validation.md` | Script ready; **MANUAL:** download EMSR838 "Observed Event" shapefile → `temp_images/emsr838_observed_event.shp`, then `python agents/validate_labels_ems.py --zone-id 000099_initial --event-year 2025`. |

### Wave 1 — T2 IN PROGRESS ⏳ (Sonnet)
- `agents/collect_field_dataset.py` — **script complete; not yet run successfully.**
- **Fix already applied by orchestrator:** FoTW loader switched from a nonexistent GEE
  asset to the proven Phase 1 method — DuckDB over Source Cooperative S3 parquet
  (`s3://ftw/global-data/predictions/vectors/alpha/results/*.parquet`, `FTW_YEAR=2024`),
  per notebook 03 "CELL 11 (WORKING)". `duckdb` 1.5.4 pip-installed. `field_id` = md5 of
  geometry WKB (idempotent upsert).
- **NEXT (run on Sonnet after reload):**
  `nohup python -u agents/collect_field_dataset.py --test > /tmp/t2_test.log 2>&1 &`
  then poll `tail -n 40 /tmp/t2_test.log`. First run installs DuckDB spatial/httpfs
  extensions + scans ~1000 parquet files — **be patient (~15 min); output is silent
  during the FoTW load** (the print uses `end=''`). After `--test` passes: review the
  VARIANCE REPORT (do **not** drop `soil_moisture`/`precip_premonsoon` on assumption),
  pixel-support distribution, per-zone `flood_pct` spread, rows written; confirm
  `field_dataset` is populated. Then run the **full multi-zone** collection.

### Waves 2–3 — PENDING (gated on `field_dataset` populated)
- T8 (Opus) `notebooks/04_terrain_baseline.ipynb` — terrain-only ranking baseline; route
  through `validation.py`; log via `tracking.py`; report §2 contract (AUC ≥ 0.65
  location-disjoint, rank stability ρ ≥ 0.6, terrain vs +seasonal). Install sklearn +
  mlflow in a notebook cell.
- T9 (Sonnet) embeddings ablation — gated on T8 in + T1 PASS + T6; keep only if lift ≥ 0.02 AUC.
- T10 (Opus) `agents/calibrate.py` — rank → field% constrained to mean = Y%; test
  calibrated RMSE vs uniform spread, location-disjoint.

### Resume checklist
1. Confirm new session picked up `settings.local.json` (subagents can run Bash).
2. Re-dispatch the **Sonnet** T2 agent to run `--test` → verify → full multi-zone run.
3. On `field_dataset` populated → launch **T8 (Opus)**; then T9/T10 per gates above.
