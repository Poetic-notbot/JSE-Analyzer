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
