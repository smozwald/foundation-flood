# Phase 2 — Embeddings Cost & Size Estimate

**Date:** 2026-06-17
**Status:** Analysis only — no inference run. Gate: embeddings are a final ablation, collected only after terrain-only baseline clears the AUC target.

---

## 1. Data volume

### 1a. Numbers from Phase 1

| Item | Value |
|---|---|
| Zone pixels (`000099_initial`) | 6,233 |
| FoTW fields in bbox (cleaned) | 24,922 |
| FoTW fields covered by cropland pixels | ~370 (1.48%) |
| Events with SUCCESS labels | 10 |
| Per-event flood rate range | 6.4% – 36.9% |

### 1b. Full-feature row sizes

One "record" is a (field, event) pair. Features planned for collection (static terrain + dynamic per-event):

| Feature group | Approx. columns | Bytes / record (float32) |
|---|---|---|
| Terrain static (elevation, slope, TWI, curvature, dist_to_channel, flow_acc, Strahler, HSG, landcover) | ~9 | 36 B |
| Dynamic per-event (NDVI pre-monsoon, soil moisture, precip pre-monsoon) | ~3 | 12 B |
| External Y% label + flood fraction label | ~2 | 8 B |
| Lat/lon | 2 | 8 B |
| **Subtotal (no embeddings)** | **~16** | **~64 B / field-event** |
| 64-dim float32 embedding (Option A or B) | 64 | **256 B** |
| **Total with embeddings** | **~80** | **~320 B / field-event** |

The embedding is **4× the rest of the feature set** per record.

### 1c. Scale comparison

| Scale | Records | No-embed total | With embeddings |
|---|---|---|---|
| ~370 covered fields × 10 events | 3,700 | ~237 KB | ~1.2 MB |
| ~6,233 sample pixels × 10 events | 62,330 | ~4 MB | ~20 MB |
| 24,922 full FoTW fields × 10 events | 249,220 | ~16 MB | ~80 MB |

**Conclusion on volume:** storage is negligible at every scale. The full FoTW × 10-event dataset with embeddings is ~80 MB — trivially storable. Volume is not the deciding factor; compute cost and temporal granularity are.

---

## 2. Option A — Annual Satellite Embedding Dataset (GEE)

**What it is:** Pre-computed annual mosaic embeddings derived from Sentinel-2/Landsat composites, available natively in GEE (e.g. the Google Research Earth Engine Annual Satellite Embeddings dataset). One 64-dim vector per pixel per year. No GPU inference required — GEE serves them as an Image Collection.

### Cost

- **GEE compute:** Free-tier GEE (non-commercial Earth Engine) covers this. A `reduceRegions` call over 24,922 FoTW field polygons extracting a 64-band image is a straightforward zonal stat. Estimated wall-clock: 5–20 minutes depending on queue; no per-query charge under the research quota.
- **Storage:** Output as a GEE Table Export to Drive/Cloud Storage. At ~80 MB for the full FoTW set, well within free-tier limits.
- **Monetary cost:** ~$0 under a non-commercial GEE account. If the project moves to a commercial GEE account, zonal stats over ~25k polygons × 1 image would be a fraction of a cent under EE pricing.

### Temporal granularity limits

- **One vector per calendar year.** The embedding is a full-year annual composite, not seasonally stratified.
- Cannot distinguish pre-monsoon vs post-monsoon signal within a year.
- For the Phase 2 use case (10 events spread across monsoon seasons 2015–2025), there is effectively **one embedding per field per year** regardless of which event within that year is being labelled.
- Events within the same year share an identical embedding — the embedding adds no within-year variation.
- In a 10-event dataset spanning roughly 5–8 distinct calendar years, Option A provides at most **5–8 distinct embedding vectors per field**, with some events sharing the same vector.

### Assessment

Cheap and fast to retrieve. Temporal granularity is too coarse for within-year event discrimination. Appropriate only as a cheap proxy for long-run landscape character (land cover type, field condition trend), not for event-specific pre-flood state.

---

## 3. Option B — Custom Seasonal Clay / Prithvi Inference

**What it is:** Running a foundation model (Clay v1 or NASA-IBM Prithvi) over Sentinel-2 seasonal composites (e.g. 3-month pre-monsoon window per event) to produce a contextually richer embedding that captures the vegetation/moisture state shortly before each flood event.

### Inference compute estimate

- **Model size:** Clay v1 / Prithvi — ~300–600 M parameters. Inference is a forward pass over a patch of satellite imagery.
- **Input:** One 256×256 pixel patch per field per seasonal window (fields are small — median 0.19 ha ≈ 2–3 Sentinel-2 pixels at 10m; in practice a standard 64×64 or 128×128 chip centred on each field is realistic).
- **Throughput on GPU (A100 / T4):**
  - Clay inference: ~50–200 chips/second on A100; ~10–40 chips/second on T4.
  - For 24,922 fields × 10 events = 249,220 inference calls:
    - A100: ~20–85 minutes of GPU time.
    - T4: ~1.7–7 hours of GPU time.
  - For the 370-field covered subset: <1 minute on any GPU.
- **Satellite image download (Sentinel-2 chips):** GEE export or direct Copernicus STAC. At 10-band × 128×128 × float32 per chip: ~655 KB/chip × 249,220 chips ≈ **163 GB** of raw image data. This is the dominant data movement cost, not the inference itself.
  - For the 370-field covered subset: ~163 MB of image data.

### Compute cost (cloud pricing)

| Hardware | Duration (full 24,922 fields) | Approximate cost |
|---|---|---|
| Google Colab Pro+ (A100, ~$50/month subscription) | 1–1.5 hours GPU | ~$2–5 marginal cost |
| GCP Vertex AI (A100 on-demand, ~$3.67/hr) | 1.5–2 hours | ~$5–7 |
| GCP Vertex AI (T4 on-demand, ~$0.35/hr) | 5–7 hours | ~$2–4 |
| **Covered 370-field subset only** | minutes | **<$0.10** |

- **Storage for raw Sentinel-2 chips:** 163 GB would cost ~$3/month on GCS; not worth storing long-term — generate chips on-the-fly and discard.
- **Embedding storage:** ~80 MB as computed above — trivial.

### Temporal granularity

- Seasonal composite (e.g. JJAS 3-month window, or 30-day pre-event composite) — one vector per (field, event), properly event-specific.
- Captures pre-monsoon NDVI, soil saturation signal, and landscape context together.
- Provides **10 distinct embedding vectors per field** (one per event), vs Option A's 5–8 shared annual vectors.

### Assessment

Meaningful added cost only for the full 24,922-field run, and even there it is low ($5–10). The primary cost is satellite image download volume (~163 GB), not inference compute. On the 370-field covered subset the cost is negligible (<$0.10, minutes of compute).

---

## 4. Recommendation

### Trade-off summary

| Criterion | Option A (Annual GEE) | Option B (Clay/Prithvi seasonal) |
|---|---|---|
| Cost | ~$0 | ~$2–10 for full dataset |
| Wall-clock time | 5–20 min | 1–7 hrs (GPU) |
| Temporal granularity | 1 vector/year (coarse) | 1 vector/event (fine) |
| Within-year discrimination | None | Yes |
| Implementation complexity | Low (GEE `reduceRegions`) | Medium (chip export + inference pipeline) |
| Semantic richness | Land cover / annual trend | Pre-flood vegetation + moisture state |

### Recommendation: Option B if embeddings are collected at all

If the terrain-only baseline fails to clear the AUC target and the ablation gate is reached, **Option B is the preferred option**. The within-year event-level granularity is the only way embeddings can add signal beyond what annual composite captures — and the cost difference ($0 vs $5–10) is immaterial at this project scale.

**However, the primary recommendation is to not collect embeddings until they are required.**

### The gate (mandatory — not advisory)

> **Embeddings are a final ablation only.**
> Do not run Option A or Option B until both of the following are true:
> 1. **Gate 1 passes:** terrain-only field-ranking AUC ≥ 0.65 under a location-disjoint split (or the honest conclusion is reached that ranking does not beat uniform spread).
> 2. **Terrain-only baseline is in:** an MLflow-tracked terrain-only run with location-disjoint CV exists.
>
> If terrain-only AUC already meets the success bar and rank stability is acceptable, embeddings are optional and should be skipped entirely. Add them only if terrain-only falls short of target and there is reason to believe pre-flood landscape state carries additional discriminative signal.

The rationale is §8.6 of the Phase 2 plan: embeddings are the costliest, least-validated lever. At the 370-field covered subset, cost is negligible — but the pipeline complexity, satellite download volume, and risk of introducing feature leakage or additional hyperparameter degrees of freedom all argue for keeping them as a last resort.

**If embeddings are added and do not produce a measurably higher AUC than terrain-only (say, ≥ 0.02 AUC lift), drop them.** Complexity without measurable benefit is negative value for a portfolio piece.

---

## Summary table

| Item | Value |
|---|---|
| Storage for full FoTW × 10 events with embeddings | ~80 MB |
| Storage for covered 370 fields × 10 events | ~1.2 MB |
| Option A retrieval cost | ~$0, 5–20 min |
| Option A temporal granularity | 1 vector/year — events within same year share embedding |
| Option B inference cost (full 24,922 fields) | ~$5–10, 1–7 hrs GPU |
| Option B inference cost (370-field covered subset) | <$0.10, <10 min GPU |
| Option B dominant cost | Sentinel-2 chip download (~163 GB full, ~163 MB subset) |
| Gate before any embedding collection | Terrain-only MLflow run + location-disjoint AUC in hand |
