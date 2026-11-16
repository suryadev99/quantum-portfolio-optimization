# Data

`qportfolio` ships no market data. Three sources are supported (`data.source`):

* `synthetic` – seeded factor-model generator with sectors, liquidity and ESG scores (default; used by all demos and tests).
* `csv` / `parquet` – a wide returns table: index = dates, one column per asset (daily simple returns).
  Optional `data.attributes_path`: one row per asset with columns `asset`, `sector`, `liquidity`, `esg`, `current_weight`.

Own μ / Σ can be supplied via `estimation.mu_path` / `estimation.sigma_path`.

Put proprietary files in this folder; `*.csv` / `*.parquet` here are git-ignored.
`reference_instances/` is reserved for small checked-in instances; the built-in reference set is generated
deterministically by `qportfolio.benchmark.reference_instances()` so nothing needs to be stored.
