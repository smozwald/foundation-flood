# Phase 2 — Restructured Plan

**Date:** 2026-06-17
**Inputs:** `instructions/phase_2_begin.md` (original brief), `reports/phase_one_analysis.md` (verified Phase 1 results).
**Framing:** This is a portfolio piece. Optimise for a *credible, honestly-validated* result, not maximum scope. Cheap go/no-go gates run first so we never invest in collection/compute before the architecture is proven viable.

---

## 1. Architecture (the thing being demonstrated)

```
user picks return period (e.g. 1-in-25-yr)
        │
        ▼
EXTERNAL hazard model ──► Y% = expected area flooded for that return period
        │
        ▼
OUR local model ──► per-field vulnerability rank (relative, dimensionless)
        │
        ▼
calibration ──► distribute Y% across fields by rank, constrained so field-average = Y%
        │
        ▼
ranked, actionable output: which fields flood first as water rises
```

- **Magnitude** comes from outside (Phase 1 proved we can't predict it locally).
- **Spatial pattern** is the only thing our model is asked to do — and only as a *ranking*, never an absolute %.
- No discharge / rainfall as model inputs (magnitude is external).

---

## 2. Success metrics (defined upfront — this is the contract)

The model is a success **only if it beats both baselines** under a location-disjoint split:

| Metric | Target | Baseline it must beat |
|---|---|---|
| Field-ranking AUC (does it flood first?) | ≥ 0.65, location-disjoint | Random (0.50) |
| Calibrated field-% error vs uniform spread | lower RMSE than uniform | **Uniform spread of Y%** (the null architecture) |
| Rank stability Cat-1 vs Cat-5 | Spearman ρ ≥ 0.6 | — |
| Terrain-only vs +embeddings | embeddings must add measurable AUC | Terrain-only model |

**If ranking does not beat uniform spread, the project's honest conclusion is "external Y% + uniform spread is the operational recommendation" — and that is still a legitimate portfolio result.**

*Validation status (2026-06-17): location-disjoint validation of these metrics is **deferred** (see §3, §7). The Phase 2 build proceeds to the field model first; these remain the eventual acceptance contract, to be checked on revisit.*

---

## 3. Execution order

> **Two checks from an earlier draft were resolved/deferred (2026-06-17):**
> - **FoTW field viability — not a concern.** The 1.4% figure was the overlap between FoTW fields and our **sparse sample pixels**, not true zone coverage; FoTW coverage over the zone itself is good. Fields are a viable unit. (During collection, ensure enough pixels per field to compute a well-supported flood-% label.)
> - **Location-disjoint ranking validation — deferred by decision.** We proceed to build the field model now and revisit whether ranking generalises across zones later. Tracked as a known open risk in §7. (LOEO leaked in Phase 1; the eventual re-validation must be location-disjoint.)

### Step 1 — External Y% source
- Pull candidate sources (priority: free/open, GEE-native): **JRC/Copernicus global flood hazard**, **Google Flood Hub / GRRR**.
- Sanity-check Y% against the strongest-signal event: the **Aug 2025 Cat-5 event** (zone flooded ≈ 37%) — does any source return a comparable order of magnitude for that return period?
- Basin-specific deep validation: **deferred** (per original brief).

### Step 2 — Collect (field level)
- Ground truth (label), field static metrics, `ndvi_premonsoon`.
- Run a **zero-variance check** on every feature *before* dropping anything — do NOT drop `soil_moisture` / `precip_premonsoon` on assumption (Phase 1 never proved they were constant; they had non-zero importance).
- Ensure adequate pixel density per field so each field's flood-% label is well-supported.

### Step 3 — Model (baseline first)
- **Terrain-only baseline model first** — establish whether cheap features clear the success bar.
- **Embeddings as a final ablation only** — collect and add last; keep only if they measurably beat terrain-only.

### Step 4 — Calibration layer
- Map ranks → field-% constrained so field-average = Y%.

---

## 4. Embeddings — cost/size estimate (decide before collecting)

64-dim float32 = **256 B / field-year**. Estimate both, costs differ:
- **Annual Satellite Embedding dataset** (cheaper, coarser, one vector/year).
- **Custom seasonal Clay / Prithvi inference** (compute cost, finer temporal control).

Also estimate full-FoTW-fields vs ~6,000-sample-pixels data volume for the whole feature set, not just embeddings. **Do not run embedding inference until Gate 1 passes and the terrain-only baseline is in.**

---

## 5. Guardrails (carried from original brief, made concrete)

- **Location-disjoint CV only** (leave-one-zone-out). LOEO alone leaked in Phase 1 — do not repeat.
- **Budget K** (how many fields flood at a given Y%) from **training/external priors, never held-out truth.**
- **Zero-variance check** on every new feature before use; drop only what's actually constant.
- **Rank-stability check:** Cat-1 vs Cat-5 ranking must be correlated (ρ ≥ 0.6).
- **Name the label circularity:** "ground truth" is Otsu-derived SAR. Acquiring even one **Copernicus EMS** event as independent validation would materially strengthen credibility — worth a small effort, not a blocker.

---

## 6. Tooling

- **Modeling notebook** with multiple model variants.
- **MLflow** tracking for every variant/run.

---

## 7. Deferred (explicitly out of scope for now)

- **Location-disjoint ranking validation (known open risk).** Does field vulnerability rank generalise across zones, and does it beat uniform spread of Y%? Deferred by decision 2026-06-17 — we build the field model first and revisit. Until checked, "ranking adds value over uniform spread" is assumed, not proven; the eventual test must use a location-disjoint split (LOEO leaked in Phase 1).
- Basin-specific validation of the external hazard source.
- Separate literature review (own track): pre-season hazard product inventory (free/open priority), precedent for downscaling coarse hazard with local features, foundation models for ag flood risk in developing countries.
- Open questions from `phase_2_begin.md` not resolved here.

---

## 8. Workflow improvements (carried forward from Phase 1)

These are process fixes, surfaced by what went wrong in Phase 1. Adopt them at the *start* of Phase 2, not in the Phase 3 scriptification step.

1. **Results must come from a tracked run, not a PDF.**
   *Why:* The Phase 1 numbers exist only in `phase-one-report.pdf`; the committed notebook's model cells were never executed and the feature set had drifted, so nothing reproduces. → Every model number in Phase 2 comes from an **MLflow run** (params + features + metric), logged from the first model, not retrofitted.

2. **Pin the feature list in code, once.**
   *Why:* The notebook and the PDF used different feature sets. → Define features in a single versioned place (a module/config), not ad-hoc per cell. Every run records exactly which features it used.

3. **One reusable validation harness — location-disjoint by default.**
   *Why:* LOEO leaked (pixel locations shared across folds → 0.991). → Write the leave-one-zone-out / spatially-blocked split **once** and route every model through it. LOEO, if used at all, is reported as a known-optimistic upper bound, never as the headline.

4. **Every feature passes a variance/summary gate before modelling.**
   *Why:* Features were proposed for dropping ("soil_moisture constant") on assumption, never checked. → A standing pre-model step prints variance + summary stats; drop decisions cite that output.

5. **Independent label validation is a first-class task, not an afterthought.**
   *Why:* JRC GSW (data ended 2021) and Copernicus EMS were referenced but never wired up, so Otsu labels are self-validated. → Schedule acquiring **one** independent validation event (e.g. a Copernicus EMS activation overlapping a study zone) early.

6. **Estimate cost before running; gate expensive steps behind cheap ones.**
   *Why:* Embeddings (Clay/Prithvi inference) are the costliest, least-validated lever. → Size data/compute up front (§4) and don't run them until Gate 1 passes and a terrain-only baseline exists.

7. **Keep the instruction-brief-first workflow.**
   *Why:* It worked in Phase 1. → `instructions/*.md` remain the spec that scripts/agents implement; this plan is one of them.

---

## Corrections folded in from Phase 1 analysis

- Pixel "AUC 0.6–0.75" was actually **0.991 and leaky** → ranking must be re-proven location-disjoint (Gate 1).
- Field magnitude R² ≈ **−0.275** (confirmed) → external Y% stands.
- "Ceiling ~0.15" → no evidentiary basis; dropped.
- "Drop constant features" → replaced with an actual zero-variance check before dropping.
