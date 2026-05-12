## Purpose
Output code from several cells in notebooks/02_database_exploration.ipynb into reproducible python files, and be populate 

Output file #1: Create Study Zone
--Cell 8. User creates study zones with different options, updating database table study_zones with unique set identifier (define_study_zones.py)
--Cell 10: OTSU Thresholding for study zones (calculate_total_flood.py)
Outputs OTSU Threshold for study zones. Create a new information table in database if not existing, linked to each study zone, quantifying the flood extent as is done in notebook. Extend to ensure this also captures a list of Sentinel scenes used to represent wet and dry.
Also log if flood capture was a FAIL or SUCCESS, as we will use SUCCESS floods only in modelling.

--Cell 11: Select study pixels. (select_study_pixels.py)
Outputs into pixels table pixels linked to each study zone. They should actually be tied also to a unique study_zone_dataset (so study zones may have multiple datasets) and thus we should also extend to include this table if not existing.
study_zone_dataset has FK=study_zone, metadata=how study pixels selected
pixel_static table to include study_zone_dataset id

select_study_pixels.py
PURPOSE: Collect sentinel grid aligned study pixels with which we will calculate flooding, as well as embeddings and other attributes to predict flooding.
INPUT: study_zone, subzones=int(default=12), reps=int(default=2) subzonewidth=int(default=500), mindist=int(default=100))
Calculate height and width of study zone (zone_size_width attribute in table)
subzones are squares of subzonewidthxsubzonedwith, placed at a minimum distance of mindist from the river centroid geometry (table rivers), randomly sampled.
Number of subzones shows how many to randomly sample.
reps = how much to repeat a subzone randomly sampled at that distance.
Sample X reps at first distance, with mindist from river. (e.g. with subzonewidth 500, we would use 350m from river as centre, so that it roughlys extends from 600-100m^2. We are not strict on edges that may overlap with river.)
Then sample another randomly selected X reps at dist+subzonewidth (e.g. 1100-600m^2 from river for our second 2 reps) until finished.
Add pixels to static_pixel if they are also classified as agriculture land per the notebook code. 
It appears pixel_static doesnt currently have a geom (which we need), ensure each pixel has a geom that can be used for satellite data.

## Testing
-Populate database only for sites in Pakistan, using 'initial' study zone sets (these exist already)
-Run select_study_pixels.py only on initial site and year to ensure proper function, once populated flood database for Pakistan.