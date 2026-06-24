# Jamaican Stock Financial Statement Analyzer

A [Streamlit](https://streamlit.io/) app for analysing companies listed on the
Jamaica Stock Exchange (JSE). Pick a company, then drill into any line item on
its Income Statement, Balance Sheet, or Cash Flow Statement to understand how
the business is built, what has changed, and what it has delivered.

## What it does

For any line item you choose, the app shows:

- **Trend chart** — how the item has changed over the last several years.
- **Growth chart** — the year-over-year change (%).
- **Makeup / share chart** — the item's share of the relevant total (e.g. a
  balance-sheet item as a % of Total Assets), so you can see the composition of
  the company and how it is shifting over time.
- **Plain-language narrative** — a read of *what* changed and, for subtotals
  like "Total Assets", *which* underlying components drove the change.
- **Ratios** — the financial ratios that the chosen item feeds into.

There is also a **Valuation** tab with a simple 2-stage discounted-cash-flow
(DCF) estimate plus an earnings-multiple cross-check.

## Data source

Financial data comes from [stockanalysis.com](https://stockanalysis.com)
(S&P Global). The app reads the structured JSON feed (`__data.json`) that backs
each financials page, decoding its compact index-pointer ("devalue") encoding
into ordinary nested data.

Companies that report in USD are flagged so per-share and valuation figures stay
sensible; a small curated list and a rough fallback exchange rate are used for
this.

## Requirements

- Python 3.8+
- The packages listed in [`requirements.txt`](requirements.txt):
  - `pandas`
  - `plotly`
  - `streamlit`

## Setup & running

Install the dependencies and launch the app:

```bash
pip install -r requirements.txt
streamlit run app.py
```

Streamlit will open the app in your browser (usually at
`http://localhost:8501`).

## Usage

1. Use the **sidebar** to select a company by its ticker.
2. Adjust the **valuation assumptions** (discount rate and long-term growth) if
   you want to explore the DCF estimate.
3. Browse the tabs:
   - **Overview** — revenue, net income, and key ratios at a glance.
   - **Income Statement / Balance Sheet / Cash Flow** — choose any line item to
     analyse in detail.
   - **Ratios** — a compact set of decision-useful ratios for the latest year.
   - **Valuation** — DCF value per share and an earnings-multiple cross-check.

## Notes & caveats

- Some JSE listings (certain funds or very new listings) are not covered by the
  data source and may not load.
- The valuation is a simplified estimate intended as a starting point for your
  own judgement — **not** a target price or investment advice.
- Data is cached for several hours to reduce load on the upstream source.
