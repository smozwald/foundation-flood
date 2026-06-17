## PURPOSE ##
Re-structure Phase 2 plan.
Analyze Phase One Results.
Consider improvements to workflow based on results and other takeaways.

## PHASE ONE REVIEW ##
notebooks/03...*.ipynb and reports/summary_colab.pdf overview phase one results.

Key takeaways:
- Pixel ranking works (LOEO AUC 0.6-0.75). Field-level magnitude regression doesn't (R² -0.29, ceiling ~0.15). Don't conflate the two.
- Field-level ranking AUC not yet tested — check by aggregating existing pixel scores to field level (no retraining) before assuming no loss.
- Local flood-extent magnitude model doesn't work — replace with external source.
- soil_moisture/precip_premonsoon are constant, drop. ndvi_premonsoon varies, keep. discharge_ts unused, not needed under new architecture.

## PHASE TWO GOALS ##
- Switch to fields (FoTW download from notebook).
- Architecture: user picks return period → external model returns Y% area flooded → our model disaggregates Y% across fields by local vulnerability rank, calibrated so field average = Y% → ranked output for action.
- External Y% source candidates: JRC/Copernicus global flood hazard maps (free, GEE), Google Flood Hub/GRRR (free). Avoid license-gated commercial products. Basin-specific check deferred.
- No discharge/rainfall needed as model input now — magnitude comes externally.
- Estimate time/data size: full FoTW fields vs ~6,000 sample pixels. Include static metrics, pct-flooded, embeddings (64-dim float32 = 256B/field-year; estimate both annual Satellite Embedding dataset and custom seasonal Clay/Prithvi inference, costs differ).
- Confirm FoTW coverage ratio (1.4%) isn't a zone-specific quirk.
- Collect: field ground truth, field static, ndvi_premonsoon, embeddings.
- Modeling notebook, multiple model variants, MLflow tracking.
- Guardrails: LOEO only, budget K from training/external priors not held-out truth, zero-variance check on new features, rank-stability check (Cat1 vs Cat5).
- Define success metrics upfront.
- Separate lit review (strong model): inventory pre-season hazard products (free/open priority), precedent for downscaling coarse hazard products with local features, foundation models for ag flood risk in developing countries. Basin specifics deferred.