# Jamaica Crop Intelligence  🌱

An integrated Streamlit app connecting Jamaican **crop production, seasonal
availability, parish sourcing, prices, threats, procurement planning** and the
full **RFQ → quotation → contract → delivery** transaction lifecycle, over an
**official-data ingestion and stewardship** layer.

> Data provenance is labelled everywhere — observed vs modelled vs illustrative
> vs registry footprint. Quarterly production is never shown as monthly output;
> the monthly supply index is a *modelled* availability shape, not a tonnage.

## Run

```bash
pip install -r requirements.txt

streamlit run streamlit_app.py          # Jamaica Crop Intelligence (default)
streamlit run app.py                     # legacy JSE stock analyzer

pytest jamaica_crop_intelligence/tests -q
```

## What's inside

- **11 pages**: Executive Dashboard, Seasonal Calendar (observed vs modelled),
  Crop Explorer, Parish Sourcing, Prices, Threats, Procurement Planner,
  Procurement Integration (RFQ→delivery + KPIs), Reports (Excel export),
  Data & Methodology, Administration (ingestion).
- **Real Jamaican domain data**: 32 crops with aliases and seasonal windows, the
  14 parishes, markets, climate/pest/disease/security threats (including praedial
  larceny), and suppliers.
- **Official-file ingestion**: upload Ministry/RADA CSV/Excel/PDF → crop-name &
  unit normalization review → data-quality flags → approve/reject → idempotent
  commit into normalized fact tables.
- **Procurement analytics**: scored quotation comparison, contract fulfillment
  KPIs, supplier performance from actual deliveries, planned-vs-actual, and
  shortages/rejections reporting.

See the module guide: [`jamaica_crop_intelligence/README.md`](jamaica_crop_intelligence/README.md).

## Screenshots

Major pages are captured in [`docs/screenshots/`](docs/screenshots/).

| Dashboard | Seasonal Calendar | Procurement Integration |
|---|---|---|
| ![Dashboard](docs/screenshots/01_dashboard.png) | ![Calendar](docs/screenshots/02_calendar.png) | ![Procurement](docs/screenshots/08_procurement_integration.png) |

## Deploy

Deploy to Streamlit Community Cloud with main file `streamlit_app.py`.
Full steps: [`output/deployment_instructions.md`](output/deployment_instructions.md).
Source register and honesty notes: [`output/source_register.md`](output/source_register.md).

---

## Legacy app — JSE Financial Statement Analyzer (`app.py`)

The original Streamlit app for analysing companies listed on the Jamaica Stock
Exchange remains available and unchanged. Pick a company and drill into any line
item on its Income Statement, Balance Sheet or Cash Flow Statement to see how it
changed over time, its growth, its share of the relevant total, a plain-language
read of what drove the change, and the ratios it feeds into.
Figures are read live from stockanalysis.com. Run it with `streamlit run app.py`.

### Valuation

The **Valuation** tab estimates what a company is worth using methods that have
endured for a very long time, each applied only where it fits the business:

- **Owner-earnings DCF (Buffett)** — net income + depreciation − maintenance
  capex, grown over ten years and discounted back (operating businesses).
- **Dividend discount (two-stage / Gordon)** — value as the growing stream of
  dividends, discounted (any reliable payer; core for banks, insurers, REITs).
- **Justified price-to-book**, `(ROE − g)/(r − g)` — book equity and the return
  on it, the right lens for financials.
- **Earnings Power Value** (Greenwald) — today's normalised earnings capitalised
  with *no* growth assumed; a deliberately conservative floor.
- **Graham Number**, `√(22.5 × EPS × book value/share)` — Graham's defensive
  ceiling, and **net-net** current-asset value as a liquidation floor.
- **Fair-P/E reversion** and **Graham's revised formula** as cross-checks.

The methods that suit the business are blended into a **central fair value** with
a low–high range, then compared to the live market price to show upside/downside
and a **margin-of-safety** buy-below line. A discount-rate × terminal-growth grid
shows how sensitive the DCF is to its assumptions. Discount rate, long-term
growth and margin of safety are all adjustable in the sidebar.
