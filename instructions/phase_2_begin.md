## PURPOSE ##
Re-structure Phase 2 plan.
Analyze Phase One Results
Consider improvements to workflow based on results and other takeaways.

## PHASE ONE REVIEW ##
notebooks/03...*.ipynb and reports/summary_colab.pdf overview phase one results.
Overview these:

Key takeaways:
- On sample site, performance of model poor.
- Very limited with pixel approach, want to go to field-based approach as not much performance loss but can incorporate more fields.
- Risk-ranking model, relies on flood model which estimates how much area will be flooded, then ranks pixels.

## PHASE TWO GOALS ##
- Switch to using fields with Fields of the World downloading seen in notebook. 
- For sample study area (000099_initial), estimate time and data size for collecting whole area of fields vs. the 6000ish sample pixels.
-Estimate should include calculation and storage of average static metrics, seasonal flood pct-flooded and 64-bit seasonal embeddings

-Collect flooded area ground truth for sample site(s) at field level.
-Collect static at field level.
-Collect dynamic seasonal model variables (premonsoon NDVI etc, framework for rs-embed to get pre-season ebeddings).

-Notebook to model variety of models with MLFlow storage

