# Task: Embeddings cost/size estimate — `reports/phase_2_embeddings_cost.md`

**Phase 2, §4** of `instructions/phase_2_plan.md`. Analysis/doc only — **no compute, no
inference run.** Decide before collecting (§8.6: estimate cost before running).

## Purpose
Size the data volume and compute cost of adding embeddings, for two options, so the
go/no-go on embeddings is an evidenced decision. **Do not run any embedding inference** —
that is gated behind Step-1 PASS *and* a terrain-only baseline being in (§4, §8.6).

## Produce a short report covering
1. **Data volume.** 64-dim float32 = **256 B / field-year**. Compute totals for:
   - full FoTW fields in the zone(s) × events, and
   - the ~6,000 sample-pixel scale (for comparison),
   for the **whole feature set**, not just embeddings.
2. **Option A — Annual Satellite Embedding dataset** (GEE): cheaper, coarser, one
   vector/year. Estimate retrieval cost/time; note temporal granularity limits.
3. **Option B — Custom seasonal Clay / Prithvi inference**: finer temporal control, real
   compute cost. Estimate GPU/inference time and storage.
4. **Recommendation** with the trade-off stated, and the explicit gate: embeddings are a
   **final ablation only**, kept only if they measurably beat terrain-only AUC (plan §2/§3).

## Out of scope
Running inference, downloading embeddings, modelling. Estimates only.
