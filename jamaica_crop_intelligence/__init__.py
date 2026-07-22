"""Jamaica Crop Intelligence.

An integrated Streamlit module connecting Jamaican crop production, seasonal
availability, parish sourcing, prices, threats, procurement planning and the
full RFQ -> quotation -> contract -> delivery transaction lifecycle, with an
official-data ingestion and stewardship layer.

Honesty principles baked into the data model:
  * Observed quarterly production is never presented as observed monthly output.
  * A modelled monthly supply index is always labelled as modelled.
  * Registry footprint (area/farmers) is never treated as live harvest volume.
  * Illustrative/seed values are flagged distinctly from official observed data.
"""

__version__ = "1.0.0"
__app_name__ = "Jamaica Crop Intelligence"
