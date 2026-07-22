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
9. **Forecast & Alerts** — modelled seasonal+trend price forecast with a ~90% band and backtest MAPE; price-spike detection, top-5 volatility watchlist, and recorded alerts.
10. **Reports** — multi-sheet Excel export.
11. **Data & Methodology** — sources, provenance, modelled relationships, honest limitations.
12. **Administration** — official-file **auto-fetch** + upload → parse → crop/unit review → data-quality flags → approve/reject → idempotent commit; plus a connectivity-diagnostics panel.

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

On the **Administration** page, two paths feed the same pipeline:

**Auto-fetch** (`ingestion/official_fetch.py`) — pick an official source and
**Fetch now**. The adapter downloads the file over HTTPS, honouring an
environment proxy + CA bundle where present, and **fails gracefully** with a
clear reason if a host is blocked (never fabricating data). It works directly
where the network allows (e.g. Streamlit Community Cloud).

**Upload** — drop a CSV / Excel / PDF for the chosen kind.

Both then run:

1. File hashed and deduped against `source_files` (records `source_name`,
   `retrieval` = fetch/upload, `origin_url`).
2. Parser fuzzy-detects columns (resilient to layout changes) and handles
   **weekly workbooks** with multiple price-type columns (farmgate / wholesale /
   urban-retail / rural-retail), melting them into typed rows; parses
   **dates / week-ending** and **markets**.
3. **Normalizes crop names** against canonical names + aliases; **maps units**
   (prices to /kg where mass-convertible); captures **lineage**
   (`original_crop_name`, `price_date`, `week_ending`, `market_location`,
   `confidence`).
4. **Data-quality validation** (`ingestion/validation.py`): drops non-positive
   values, flags IQR outliers and suspected unit mismatches into
   `data_quality_flags`.
5. Review preview + crop/unit review lists + flags, then **Approve** (idempotent
   commit deduped by `record_key` incl. date + market) or **Reject** (writes
   nothing). Everything is logged into `import_runs` and `data_quality_flags`.

The schema self-migrates: new lineage columns are added to an existing database
via idempotent `ALTER TABLE` on startup (`database/schema.apply_migrations`), so
a warm-restarted deployment is never left on a stale schema.

> Note: some official hosts may be unreachable under a restricted network
> policy (the build sandbox blocks moa.gov.jm / data.gov.jm). Auto-fetch reports
> this honestly; the same files can be downloaded and uploaded instead. See
> [`output/source_register.md`](../output/source_register.md).

## Forecasting & alerts

- **Forecast** (`calculations/forecast.py`, `services/forecast_service.py`): an
  OLS linear trend + additive seasonal factors with an empirical ~90% band, and
  a backtest MAPE. Dependency-light (numpy/pandas — no statsmodels/Prophet) so it
  deploys cleanly. Always labelled a **modelled estimate**; runs on seed prices
  now and ingested official prices later.
- **Alerts** (`services/alert_service.py`): period-on-period price-spike
  detection at a configurable threshold, a top-N volatility watchlist, and alerts
  recorded into the `alerts` table (deduped by title).

### Daily notifications (external notifier)

The app *detects and records* alerts; it never claims to send a notification it
cannot. To deliver a daily digest, run the standalone notifier on a schedule:

```bash
# prints JSON; POSTs to $ALERT_WEBHOOK_URL if set (Slack/Teams/Make webhook)
python scripts/price_alert_notifier.py --threshold 15 --top 5 --fetch
```

A ready-to-enable GitHub Action is provided at
`.github/workflows/price-alerts.yml.example` (rename to `.yml`, add an
`ALERT_WEBHOOK_URL` secret, uncomment the daily `schedule`). GitHub runners have
open internet, so `--fetch` can pull official prices there even though a
restricted sandbox cannot. Email delivery requires an email provider/credential
configured by the operator (not bundled).

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
