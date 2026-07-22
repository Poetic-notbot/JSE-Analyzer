# Jamaica Crop Intelligence

An integrated Streamlit module connecting Jamaican **crop production, seasonal
availability, parish sourcing, prices, threats, procurement planning** and the
full **RFQ → quotation → contract → delivery** transaction lifecycle, over an
**official-data ingestion and stewardship** layer.

Entry point: [`streamlit_app.py`](../streamlit_app.py) at the repository root.

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py          # this app
streamlit run app.py                     # legacy JSE stock analyzer (unchanged)
pytest jamaica_crop_intelligence/tests -q
```

## Honesty principles (built into the data model)

Every numeric fact carries a **provenance** tag, surfaced as a coloured badge
throughout the UI:

| Provenance | Meaning |
|---|---|
| `official_observed` | Official observed (Ministry / RADA / STATIN) |
| `official_registry` | Registry footprint — **not** live harvest availability |
| `modelled` | Research-supported modelled relationship |
| `illustrative` | Seed placeholder — **not** official |
| `user_entered` | Entered in-app |

Non-negotiables enforced in code and copy:

- Quarterly production is **never** presented as observed monthly output.
- The monthly **supply index** is a *modelled availability shape* (0–1), never a
  tonnage.
- Registry footprint (farmers / area) is **not** treated as live harvest volume.
- Prices are **not fabricated** when official files fail — seeds are labelled
  *illustrative* and are meant to be replaced by ingested official files.

## Pages

1. **Executive Dashboard** — top crops, price volatility, in-season & active-threat snapshot.
2. **Seasonal Calendar** — side-by-side *observed quarterly production* vs *modelled monthly supply index*.
3. **Crop Explorer** — profile, aliases, seasonality, prices, sourcing, threat exposure.
4. **Parish Sourcing** — registry footprint across the 14 parishes.
5. **Prices** — farmgate JMD/kg trends and volatility.
6. **Threats** — climate / pest / disease / security seasonality heatmap (incl. praedial larceny).
7. **Procurement Planner** — demand → required quantity (buffer + post-harvest loss) → local supplier coverage, with supply-shock what-ifs.
8. **Procurement Integration** — RFQ → quotation (scored comparison) → contract award → delivery recording → fulfillment KPIs, supplier performance, planned-vs-actual, shortages & rejections.
9. **Reports** — multi-sheet Excel export.
10. **Data & Methodology** — sources, provenance, modelled relationships, honest limitations.
11. **Administration** — official-file ingestion: upload → parse → crop/unit review → data-quality flags → approve/reject → idempotent commit into normalized fact tables.

## Architecture

```
jamaica_crop_intelligence/
  config/settings.py          paths, parishes, provenance vocab, source registry
  database/schema.py          all tables + connection helpers
  database/seed.py            curated Jamaican reference data (+ demo procurement)
  calculations/core.py        pure, unit-tested functions (no I/O)
  services/repository.py      thin SQLite data-access layer
  services/*_service.py       production, price, threat, procurement,
                              procurement_integration, scenario, import
  ingestion/ministry_ingestion.py  resilient CSV/XLSX/PDF parser + normaliser
  exports/excel_export.py     xlsx workbook builder
  components/ui.py            shared Streamlit widgets (provenance badges etc.)
  tests/                      test_core, test_ingestion_and_procurement, test_workflows
```

The database is SQLite. On Streamlit Community Cloud the filesystem is
ephemeral, so the DB is created and **re-seeded on each cold start**; set
`JCI_DB_PATH` to a persistent volume to retain ingested data between restarts.

## Official-data ingestion (end to end)

On the **Administration** page:

1. Pick the file **kind** (`prices`, `production`, `area_reaped`, `registry`).
2. Upload a CSV / Excel / PDF (the file is hashed and deduped against
   `source_files`).
3. The parser fuzzy-detects columns (resilient to Ministry layout changes),
   **normalizes crop names** against canonical names + aliases, **maps units**
   (converting prices to /kg where mass-convertible), and raises
   **data-quality flags** (unmatched crops with suggestions, unknown units,
   range checks).
4. Review the normalized preview, crop/unit review lists, and flags.
5. **Approve** to commit into normalized fact tables — commits are **idempotent**
   (deduped by deterministic `record_key`) — or **Reject** to write nothing.
   Every step is logged into `import_runs` and `data_quality_flags`.

> Note: some official hosts may be unreachable under a restricted network
> policy. The same files can always be downloaded by the user and uploaded
> manually here. See [`output/source_register.md`](../output/source_register.md).

## Modelled relationships

- **Supply index**: peak = 1.0 at harvest months, 0.4 shoulder in adjacent
  months, small baseline elsewhere.
- **Price response**: %ΔPrice = %ΔQuantity / elasticity (default −0.6).
- **Threat exposure**: impact × seasonal intensity × in-season availability.
- **Required procurement**: demand × (1 + buffer) / (1 − loss).
- **Quotation score** (0–100): price 45%, lead time 20%, quality 20%,
  reliability 15% (weights configurable).

## Limitations (honest)

- Seeded production and prices are **illustrative** until official files are
  ingested; they exist so the app is usable on first run and are labelled as
  such everywhere.
- The monthly supply index is a modelled *shape*, useful for planning windows —
  it is not a forecast of tonnage.
- Registry footprint reflects where farmers/area are registered, not current
  harvest availability.
- PDF table extraction depends on the source PDF's structure; unrecognised
  layouts are flagged for manual mapping rather than guessed.
