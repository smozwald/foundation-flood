## Purpose ##
Overview current status and output a report in reports/phase_one_summary.pdf.
Also output in notebooks, 03_initial_model.ipynb (to use with colab, so include colab code to get variables and supabase as in other notebooks, i will load and run there so just ensure it looks good).
Review and re-plan phase two.

## Phase One Outputs ##
Attempted to map flooded pixels on agri level in zone_id=000099_initial in Pakistan.
Lessons: This will be hard to scale to different rivers.
Particularly with low computer power
REPORT/NOTEBOOK PLANNING:

- Check online on AI4GOOD for flood mapping, see if there is a better/simpler way to collect historical data on flooded pixels we can use as our ground truth, compare with current method (in text)
- Look at current data setup for modelling pixels, analyze for strengths and weaknesses.
- Assess feasability of moving to field-mapping using fields of the world. Pros/Cons vs pixel approach.
- Generate and include figures showing mapped flooded area for different flood events in this zone, Showing flooded agri in blue vs red for unflooded agri. (output to temp_images/ and make sure these are in gitignore), overlain on satellite image. Also show date of flood and percentage_flooded calculation.

## Notebook for Phase Two Planning and initial model ##
- In study region, compare selected pixels vs fields of the world (use 2025 basemap for all years, >70% confidence). For each field output the percentage of field flooded in each event. From start of flood calculate total inundation time as well (Due to revisit time of satellites, if its flooded day 1 but not 4 put 1-4 days). So total flooded: X%, flooded A-B days: Y%, flooded B-C days, etc...
- Compare inundation method calculated vs online calculation using developed tools (AI4GOOD, but also check others), to assess flooded area in target flood events. Check diff with mine and try to include a solid ground truth e.g. from optical).
- If ground truth exists, I will try to calculate per-field in each event, and assess if easier to just use online source on cloud-processing. 

From this point I will return with more instructions, ensure cells allow me to make decision on how to proceed and if these sources/tools are better and more convenient