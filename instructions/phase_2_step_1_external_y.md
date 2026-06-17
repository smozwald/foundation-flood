# Task: External Y% hazard source — `agents/fetch_external_y.py`

**Phase 2, Step 1** of `instructions/phase_2_plan.md` (§3). First **go/no-go gate** —
must pass before any field collection (Step 2) or embedding compute (§4).

## Purpose
For each study zone, pull **return-period flood hazard** from candidate external
products via GEE, compute the zone-inundated fraction **Y%** at each return period,
store the Y%(return period) curve in `external_flood_hazard`, then **sanity-check**
those curves against observed event flood fractions in `zone_flood_analysis`.

Phase 1 proved we cannot predict magnitude locally (field R² = −0.275, see
`reports/phase_one_analysis.md`). The whole architecture depends on sourcing Y%
externally, so this script must show at least one free/open source returns a
credible Y% curve. **It does not model anything** — acquisition + a sanity gate only.

## The gate
> **PASS:** for ≥1 source, Y%(return period) is monotonic and its high-RP end is the
> same order of magnitude as the strongest observed event — **Aug 2025 Cat-5, zone
> ≈ 36.9% flooded**. I.e. some RP yields Y% in the tens-of-percent range, not <1% nor >90%.
>
> **FAIL:** no source returns a plausible curve → **stop, do not start Step 2**; record finding.

Print `GATE: PASS|FAIL` per source + overall verdict. No RP→category mapping, no
basin-specific validation (deferred, §3/§7) — order-of-magnitude credibility only.

## Candidate sources (free/open, GEE-native; `--source` selector, all by default)
1. **JRC Global River Flood Hazard** — `JRC/CEMS_GLOFAS/FloodHazard/v1`. One image per
   RP (10/20/50/100/200/500); band = flood depth (m). Inundated = depth>0. **Verify
   asset ID + band names against the live GEE catalog before sampling.**
2. **Google Flood Hub / GRRR** — if a return-period/inundation product is in GEE for the
   region, compute the same curve. If API-only (not GEE), record as limitation and skip;
   do not block the gate on it.

## Computation (per zone × source × RP)
```
mask   = depth > 0
Y_frac = pixelArea-weighted mean of mask over zone geom = inundated_area / zone_area
Y_pct  = 100 * Y_frac
```
Use `reduceRegion` at the product's **native scale** + `tileScale`; never reproject finer
than native. Keep raw RP integer; do not collapse to category.

## New table
```sql
CREATE TABLE IF NOT EXISTS external_flood_hazard (
    zone_id            text    NOT NULL REFERENCES study_zones(zone_id),
    source_product     text    NOT NULL,
    return_period_yr   integer NOT NULL,
    y_pct              double precision NOT NULL,
    native_scale_m     double precision,
    source_metadata_id uuid REFERENCES data_sources(source_id),
    computed_at        timestamptz DEFAULT now(),
    PRIMARY KEY (zone_id, source_product, return_period_yr)
)
```
`ON CONFLICT (zone_id, source_product, return_period_yr) DO UPDATE SET y_pct=EXCLUDED.y_pct, native_scale_m=EXCLUDED.native_scale_m, computed_at=now()`.

## Sanity check
Per (zone,source): load observed SUCCESS flood fractions from `zone_flood_analysis`
(Cat-1 ~6.4–17.7%, n=9; Cat-5 36.9%, n=1). Print the RP curve next to the observed range,
test monotonicity (epsilon-tolerant) and existence of an RP with Y% ∈ [~10%,~60%]. Emit GATE line.

## data_sources registration
Upsert one row per product (`ON CONFLICT (product_name) DO UPDATE`), return `source_id`
as FK. `methodology_desc` states: GloFAS RP depth, Y% = area-weighted fraction depth>0,
sanity-checked vs zone_flood_analysis, not basin-calibrated.

## Flags
`--zone-pattern` (regex, default `.*`), `--zone-id`, `--source {jrc|floodhub|all}`,
`--test` (first zone × jrc; print curve+gate; still writes), `--dry-run` (compute+print, no insert).

## Conventions (CLAUDE.md)
`SUPABASE_CONN_STRING` via `.env`; `with psycopg2.connect(...)`; `%(name)s` params; stdout
progress. Exit 0 on success (a `GATE: FAIL` is a completed run → exit 0); exit 1 only on error.
Wrap each (zone,source) in try/except; final summary line.

## Run order
`define_study_zones.py` + `calculate_total_flood.py` → **`fetch_external_y.py`** → (if PASS) Step 2.

## Out of scope
RP→category calibration (Step 4), basin-specific validation (§7), any field/embedding/model work.
