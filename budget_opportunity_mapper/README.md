# Jamaica Budget Opportunity Mapper

A web-first, PDF-fallback Streamlit app that maps the **full Jamaica government budget**
into a single **reconciled opportunity tree** -- accounting for every dollar without
double-counting -- and shows where contractors, suppliers, consultants, SMEs,
public-body investors, financiers and citizens can access value.

> This project lives in its own folder and is completely independent of the
> JSE stock analyzer in the repository root. Nothing in the root app is changed.

---

## How to run

\u0060\u0060\u0060bash
cd budget_opportunity_mapper
python -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
streamlit run app.py
\u0060\u0060\u0060

Then in the app:

* **Web-first mode** (default): open a source in the sidebar (Estimates of
  Expenditure, Fiscal Policy Paper, Public Bodies Estimates, etc.), pick the parent
  node to attach lines under, and click **Fetch & extract**. Extracted lines go to
  the **review queue** -- they are NOT added to totals until you accept them.
* **Upload mode** (fallback): upload your own budget PDF and extract from it.

Run the tests:

\u0060\u0060\u0060bash
cd budget_opportunity_mapper
pytest -q
\u0060\u0060\u0060

---

## Data model (every figure is one of five node types)

| Type | Meaning | Counts toward totals? |
|------|---------|-----------------------|
| \u0060ROOT\u0060 | the single master total | yes (as the apex) |
| \u0060PARENT\u0060 | reconciles to its children | no (sum of children) |
| \u0060CHILD\u0060 | leaf money, belongs to exactly one parent | yes |
| \u0060BALANCE\u0060 | **computed remainder**, not new money | yes (as a leaf) |
| \u0060CROSS_CUT\u0060 | analytical view only (e.g. "health opportunities") | **never** |

Every figure carries full provenance: source title, source URL, page number (if PDF),
quote/snippet, confidence score, and extraction method
(\u0060web_pdf\u0060 / \u0060uploaded_pdf\u0060 / \u0060manual\u0060 / \u0060computed\u0060).

---

## How reconciliation works

For every \u0060ROOT\u0060/\u0060PARENT\u0060 node the engine compares the **parent amount** to the
**sum of its (non-cross-cut) children** and reports:

* parent amount
* children sum
* difference
* status: **OK** (<=0.5%), **WARNING** (<=2%), **ERROR** (>2%)

See \u0060budget_mapper/reconciliation.py\u0060. The dashboard shows a parent-vs-children
chart, a status table, and a waterfall bridging *Central Government + Public Body
Capex -> Master Total*.

## How double-counting is prevented

1. **Single ROOT** -- exactly one master total (\u0060validate_single_root\u0060).
2. **Single parent** -- every non-root node has exactly one existing parent, and the
   graph is acyclic (\u0060validate_single_parent\u0060).
3. **ROOT = sum of direct children** (\u0060validate_root_equals_children\u0060).
4. **Leaf-sum check** -- the sum of all leaf money (\u0060CHILD\u0060 + \u0060BALANCE\u0060) under the
   root must equal the root, so a parent is never counted *and* its children
   (\u0060validate_no_double_counting\u0060).
5. **Cross-cut isolation** -- \u0060CROSS_CUT\u0060 nodes are excluded from every total and
   every reconciliation (\u0060validate_cross_cut_excluded\u0060).
6. **Balances are computed** -- \u0060BALANCE\u0060 nodes are derived as
   \u0060parent - sum(other children)\u0060 and flagged \u0060is_computed_balance\u0060
   (\u0060validate_balance_marked\u0060).
7. **Public-body capex is separate** -- it sits under its own parent and is only
   consolidated into the master total at the root, clearly labelled.

Business rules baked into classification: compensation of employees and debt
service are **non-opportunities**; welfare transfers are **citizen/welfare**, not
procurement; public-body capex is a **separately labelled** opportunity.

---

## How to add sources

Edit \u0060config/sources.json\u0060. Each entry:

\u0060\u0060\u0060json
{
  "id": "estimates_expenditure",
  "title": "Estimates of Expenditure (Annual & Supplementary)",
  "url": "https://www.mof.gov.jm/resources-annual-and-supplementary-estimates/",
  "description": "...",
  "is_pdf": false,
  "max_pages": 50
}
\u0060\u0060\u0060

Set \u0060"is_pdf": true\u0060 and point \u0060url\u0060 at a direct \u0060.pdf\u0060 link (copied from the MOFPS
document hub) to enable **Fetch & extract**. The included URLs were verified live on
\u0060mof.gov.jm\u0060.

## How to verify figures

1. Fetch/extract a PDF -> lines land in the **review queue** (tab "Tables & Review").
2. Each item shows the value, page number, source title and snippet.
3. Click **Accept** to add it as a *verified* node, or **Reject** to discard.
4. Accepting **never overwrites** existing verified values -- it appends a new
   verified node, so the ledger stays auditable.
5. Re-check the reconciliation banner; fix any WARNING/ERROR before relying on totals.

---

## Folder structure

\u0060\u0060\u0060
budget_opportunity_mapper/
  app.py                       Streamlit dashboard
  requirements.txt
  README.md
  config/sources.json          official Jamaica sources (verified URLs)
  data/{raw,uploads,reviewed}/  PDFs and exports
  budget_mapper/
    __init__.py
    models.py                  Pydantic models + structural invariants
    ingest.py                  web-first + upload ingest, review-queue gate
    extractors.py              page-level pdfplumber / PyMuPDF extraction
    reconciliation.py          reconciliation engine + validators
    classification.py          opportunity access-path classification
    visuals.py                 Plotly charts
    profiles/jamaica_fy2026_27.py   seed reconciled tree (illustrative)
  tests/
    conftest.py
    test_reconciliation.py
    test_no_double_counting.py
\u0060\u0060\u0060

## Disclaimer

The seed figures in \u0060profiles/jamaica_fy2026_27.py\u0060 are **illustrative placeholders**
to demonstrate the engine. Replace them with verified figures from the official
Estimates of Expenditure and Public Bodies Estimates via the review queue before
using any numbers for decisions.
