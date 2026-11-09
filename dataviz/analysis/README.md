# `dataviz/analysis/`: the Data Analysis zone

SQL and NoSQL queries on the user's own connections. `service.py` is the one entry point: routes and assistant tools call it.

| File | What it does |
|---|---|
| `classify.py` | The safety gate. A statement runs at once only when it is proven to be a plain read; anything else, including anything that cannot be parsed, needs a confirm. |
| `adapters.py` | One interface over PostgreSQL, SQL Server and MongoDB: databases, tables, describe, streamed run, explain, cancel. |
| `spool.py` | Results go to a Parquet file. The grid, statistics and charts read that file through DuckDB, so they never query the user's database again. |
| `forecast.py` | A measure over time, forecast ahead from the saved result: steps built and filled in DuckDB, four classic models (naive, seasonal naive, Holt, Holt-Winters, on numpy and scipy) tested on held-back history, an 80% / 95% range, warnings and unusual steps. |
| `service.py` | Sessions, the confirm token, runs, temp tables on a pinned connection, history, scripts, dashboards. |

Rules: sessions are private and never shared, because free SQL runs with the owner's credentials. Rows never go to the AI in bulk. Every confirmed change is written to `audit_log`. Nothing here imports the visualizer or labeling.
