# Task: Independent label validation (Copernicus EMS) — `agents/validate_labels_ems.py`

**Phase 2 side-track** (`phase_2_plan.md` §5, §8.5). First-class task, not an afterthought.
Small effort, materially strengthens credibility — **not a blocker** for the build.

## Purpose
Our "ground truth" is Otsu-derived SAR — self-validated (`reports/phase_one_analysis.md`,
caveat 3: JRC GSW ended 2021, Copernicus EMS shapefiles never uploaded). Acquire **one**
independent flood-extent event (a **Copernicus EMS** activation) overlapping a study zone and
compare it against our Otsu labels to quantify agreement.

## What to build
1. Identify a Copernicus EMS rapid-mapping activation overlapping a study zone + event window
   (ideally the Aug 2025 Cat-5). Download its flood-extent vector/raster.
2. Rasterise/align to our pixel grid; compute agreement vs `pixel_flooded.is_flooded`:
   IoU, precision/recall, % agreement.
3. Write a short `reports/phase_2_label_validation.md`: which event, source URL, the agreement
   metrics, and a plain statement of how much to trust the Otsu labels.

## Conventions
CLAUDE.md: `.env`, `%(name)s`, stdout, exit 0/1, `--test`/`--dry-run`. If no suitable EMS
activation exists for the zones, say so explicitly and record the gap — do not fabricate.

## Out of scope
Re-labelling the dataset, modelling. This quantifies label trust; it does not change labels.
