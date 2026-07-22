# Source Register — Jamaica Crop Intelligence

Verified official and guidance sources used by the ingestion layer and the
Data & Methodology page. Ingest the tabular/PDF files on the **Administration**
page; the parser normalizes crop names and units and commits into normalized
fact tables.

| Source | URL | Kind | Provenance |
|---|---|---|---|
| MOA Agricultural Data portal | https://www.moa.gov.jm/content/agricultural-data | portal | official observed |
| MOA Commodity Prices | https://www.moa.gov.jm/document-categories/commodity-prices | prices | official observed |
| All-Island Farmgate Prices by Quarter | https://www.moa.gov.jm/agridata/all-island-estimates-farmgate-prices-quarter | prices | official observed |
| All-Island Crop Area Reaped by Quarter | https://www.moa.gov.jm/agridata/all-island-estimates-crop-area-reaped-quarter | area_reaped | official observed |
| Crop Production by Quarter 2024 (PDF) | https://www.moa.gov.jm/sites/default/files/crop_production_by_quarter_2024.pdf | production | official observed |
| Crop Production by Quarter 2022 (PDF) | https://www.moa.gov.jm/sites/default/files/crop_production_by_quarter_2022.pdf | production | official observed |
| Crop Production 10yrs 2015–2024 (PDF) | https://www.moa.gov.jm/sites/default/files/crop_production_10yrs_2015-2024.pdf | production | official observed |
| RADA/ABIS public crop registry (CSV) | https://data.gov.jm/sites/default/files/reportfarmers_parameterized_crop_summary-xlsx.csv | registry | official registry (footprint) |
| FAO–RADA crop-calendar initiative (context) | https://www.fao.org/jamaica-bahamas-and-belize/news/detail-events/zh/c/1709422/ | guidance | modelled/context |

## Extended source inventory (verified layouts)

| Source | Access | Format | Notes |
|---|---|---|---|
| MoA Agricultural Data (crop production/area/farmgate, 10-yr & by-quarter, export crops, livestock) | `moa.gov.jm/sites/default/files/<dataset>_<year>.pdf` | PDF (2016–2025) | Auto-fetch (browser UA + certifi). Table extraction varies; review flagged rows. |
| MoA Commodity Prices — weekly | `moa.gov.jm/sites/default/files/pdfs/<Type>%20<MM.DD.YYYY>.xlsx` | XLSX (weekly) | **Preferred over PDFs.** Dated URLs rotate weekly → download + Upload. Types: Farmgate, Wholesale, Retail, Rural Retail, Rural Retail Meat, Retail Meat, Urban Municipal. |
| RADA/ABIS registry | `data.gov.jm/.../reportfarmers_parameterized_crop_summary-xlsx.csv` | CSV | Registry footprint (not live harvest). |
| FAOSTAT QCL (Jamaica, area=109) | `fenixservices.fao.org/faostat/api/v1/en/data/QCL?area=109&element=5510&output_type=objects` | JSON API / bulk CSV | **Cleanest programmatic source.** Annual. elements: 5510 production, 5312 area, 5419 yield. Dedicated in-app adapter. |
| JAMIS | `ja-mis.com` | ASP.NET site | Live weekly price chain (farmgate→wholesale→retail). Needs session/viewstate handling — not yet automated; use for reference. |
| STATIN | `statinja.gov.jm` | Portal | Official production/GDP series for cross-check. |
| World Bank Open Data | API | JSON/CSV | Macro agricultural indicators. |

### MoA weekly XLSX layouts (the parser branches on report type)

- **Farmgate / Rural Retail** — parish-based. Multi-row header; parish names are
  **merged cells** spanning 5 metric columns (Low, High, Most Frequent, Supply,
  Grade). Cols A/B = Commodity, Variety/Source. Missing = `"-"`. Footer
  ("Prepared on …") dropped. Parser reads merged ranges, takes **Most Frequent**
  as the price, unpivots to `(week_ending, commodity, variety, parish, price)`.
- **Retail / Wholesale** — Kingston Metropolitan Region supermarket layout with
  an **Average Price** column plus per-store location columns. Parser uses
  Average Price.
- Report type detected from filename/title; header newlines/whitespace stripped.

### FAOSTAT element/taxonomy mapping

`element` 5510=Production (t), 5312=Area harvested (ha), 5419=Yield. FAOSTAT item
names (e.g. "Yams", "Chillies and peppers, green") are mapped to the app's crop
taxonomy in `ingestion/faostat.FAOSTAT_ITEM_TO_CANON`; unmatched items are
surfaced for review, never invented.

## The SSL certificate issue (fixed)

Auto-fetch of `moa.gov.jm` on Streamlit Cloud failed with
`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`. This is a
**TLS-trust** problem (the server was reached; its chain wasn't verifiable),
**not** a network block. Fix shipped:

- Verification is routed through an explicit CA bundle — the environment's
  `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` when present, otherwise **certifi** —
  never disabled.
- `requests>=2.31` and `certifi>=2024.2.2` pinned in `requirements.txt`.
- The in-app error is now **classified** (`cert_trust` vs `timeout` vs
  `proxy_block`/`forbidden` vs `network`) so it no longer mislabels a cert
  failure as a network block.
- If a host serves an *incomplete chain* (missing intermediate), certifi alone
  won't fix it — the Upload tab remains the permanent fallback.

## Network note

In the build environment used to author this module, outbound access to
`data.gov.jm` (and likely `moa.gov.jm`) was **blocked by the egress policy**, so
these files could not be fetched server-side. This is expected and handled:

- The app **never fabricates** official values. Until a file is ingested,
  production/price figures are clearly labelled **illustrative**.
- Download any file above in a browser and upload it on the **Administration**
  page — parsing, normalization, review and idempotent commit work fully offline.
- On a network policy that permits these hosts, the same URLs can be fetched
  directly.

## Provenance discipline

- **Farmgate prices / area reaped / production by quarter** → observed facts.
- **RADA/ABIS registry** → *footprint* (registered farmers/area), explicitly not
  live harvest availability.
- **FAO–RADA crop calendar** → used as *guidance* informing the modelled monthly
  supply index, not as observed monthly output.
