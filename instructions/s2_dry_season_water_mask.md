# Instruction: Sentinel-2 Dry-Season Water Mask

## Goal

Extend `calculate_total_flood.py` to derive a **per-event, observed water mask** from
Sentinel-2 MNDWI during the pre-seasonal dry window, and union it with the existing JRC
permanent-water mask before the OTSU thresholding step. This replaces reliance on the
static JRC historical mask alone with a year-specific water map grounded in actual
dry-season imagery.

## Background

`calculate_total_flood.py` already computes a dry window (Feb 15 – May 15 of the year
prior to each flood event) to build an S1 SAR composite. The JRC permanent-water mask
`perm_water_mask = jrc_water.select('seasonality').gte(10).Not()` is then applied when
computing the OTSU change histogram. The problem: JRC reflects a multi-year historical
average and can miss or misplace water bodies in any given year.

Sentinel-2 optical imagery over Feb–May (Pakistan dry season) has low cloud cover.
A cloud-masked median composite of MNDWI over that window reliably maps open water
without flood contamination.

## What to add

### New GEE helper: `build_s2_dry_water_mask(zone_geom, dry_start, dry_end)`

- Filter `COPERNICUS/S2_SR_HARMONIZED` to the zone and the dry window dates.
- Apply the QA60 cloud mask: set pixels where QA60 bit 10 (opaque clouds) or bit 11
  (cirrus) is set to masked.
- Compute MNDWI per image:
  `MNDWI = (Green − SWIR1) / (Green + SWIR1)`
  where Green = B3, SWIR1 = B11 (scaled: divide raw DN by 10000 first).
- Take the **median** composite across all cloud-masked scenes in the window.
- Threshold: pixels where `median MNDWI > 0.0` are water.
- Return the resulting binary mask (1 = water, 0 = not water), or `None` if the
  collection is empty after cloud masking.

### Integration in `compute_flood_mask`

Change the signature to accept an optional `s2_water_mask` argument (default `None`).

Inside `compute_flood_mask`, before computing the histogram:

```python
if s2_water_mask is not None:
    combined_no_water = perm_water_mask.And(s2_water_mask.unmask(0).Not())
else:
    combined_no_water = perm_water_mask
```

A pixel passes through only if it is non-water in **both** JRC and S2.
`unmask(0)` ensures cloudy/missing S2 pixels default to non-water and are not
incorrectly removed from the histogram.

Pass `combined_no_water` where `perm_water_mask` is currently passed to
`_get_histogram`.

### Call site in `main()`

After building `dry_db` and before calling `compute_flood_mask`, call:

```python
s2_water = build_s2_dry_water_mask(zone_geom, dry_start, dry_end)
if s2_water is None:
    print("S2 water mask: empty — JRC only")
else:
    print("S2 water mask: OK")
```

Pass `s2_water` through to `compute_flood_mask`.

### Logging

Add a boolean column `s2_water_mask_used` (boolean, default false) to
`zone_flood_analysis`. Set it to `True` when `s2_water` is not `None`. No schema
migration needed — use `ADD COLUMN IF NOT EXISTS` in `ensure_table`.

## Parameters / thresholds

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| MNDWI threshold | 0.0 | Standard open-water cutoff; conservative (avoids moist soil) |
| Cloud mask bits | QA60 bits 10 & 11 | Standard S2 cloud + cirrus flags |
| `unmask(0)` on S2 result | required | Treat cloudy/missing pixels as non-water so they don't incorrectly mask the OTSU histogram |

## Edge cases

- **Empty S2 collection** (no scenes in window): fall back gracefully to JRC-only mask,
  log "S2 water mask: empty — JRC only", set `s2_water_mask_used = False`.
- **All pixels cloud-covered**: `unmask(0)` handles this — cloudy pixels default to
  non-water, so no OTSU histogram pixels are incorrectly removed.
- **Scale**: compute S2 mask at 20 m (native SWIR resolution); the OTSU histogram
  already runs at 100 m scale so no mismatch issue.

## Do not change

- The OTSU algorithm, thresholds, valley ratio check, or flood pixel counting logic.
- The S1 dry composite or wet scene selection.
- The existing JRC mask — it remains in the union; S2 adds to it, does not replace it.
- The `--test` flag behaviour.
