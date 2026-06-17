# Phase 2 — Label Validation Report

**Generated:** 2026-06-17  
**Script:** `agents/validate_labels_ems.py`  
**Study zone:** `000099_initial`  

---

## Purpose

Our flood labels are Otsu-derived SAR (self-validated). This report quantifies agreement between those labels and an independent Copernicus EMS Rapid Mapping flood-extent product for a matching event window. It does not change the labels — it establishes how much to trust them.

---

## Gap — No independent validation performed

No Copernicus EMS shapefile was found.

The script searched `temp_images/` for files matching `emsr*.shp` or `emsr*.gpkg` and found none. Network access to the Copernicus EMS portal is not available in this environment (the EMS download service requires a browser session / authentication; there is no stable unauthenticated download URL).

**To resolve:** manually download the 'Observed Event' product from one of the candidate activations below, extract the shapefile, and place it in `temp_images/` as e.g. `emsr838_observed_event.shp`. Then re-run this script.

---

## Known candidate EMS activations

The following Copernicus EMS activations were identified as overlapping the study area and event windows. They have **not** been downloaded or compared — this is the gap recorded here.

### EMSR838

- **Event:** Pakistan floods Aug 2025 (Chenab / Indus basin)
- **Year:** 2025
- **URL:** https://emergency.copernicus.eu/mapping/list-of-components/EMSR838
- **Status:** shapefile not available in this environment

### EMSR629

- **Event:** Pakistan floods 2022 (Chenab / Indus basin)
- **Year:** 2022
- **URL:** https://emergency.copernicus.eu/mapping/list-of-components/EMSR629
- **Status:** shapefile not available in this environment

## Implication for label trust

Without independent validation, the Otsu-derived labels remain **self-validated only**. The label circularity noted in `reports/phase_one_analysis.md` (caveat 3) is unresolved.

**Recommended action:** Download the observed-event shapefile from one of the candidate activations above, place it in `temp_images/` as `emsr838_observed_event.shp` (or `.gpkg`), and re-run this script.

---

_No metrics were computed. This file records the gap, not fabricated results._
