Project schema — Sports Predictions

Overview
- Goal: ingest match & odds data, build features, train models (O/U 2.5, BTTS), backtest, and serve predictions via an API and frontend dashboard.

Monorepo layout (planned)
- /backend/                -> Node/Express or FastAPI service for serving models, orchestration, auth, and admin APIs
- /frontend/               -> React + Vite app for dashboards, model insights and backtesting UI
- /src/                    -> Python data pipeline, scrapers, feature builders, training, utils
  - /src/scrapers/         -> SofaScore scraper, other source adapters ([src/scrapers/sofascore_scraper.py](src/scrapers/sofascore_scraper.py))
  - /src/odds/             -> Odds parsing and collectors ([src/odds/odds_collector.py](src/odds/odds_collector.py), [src/odds/oddsportal_parser.py](src/odds/oddsportal_parser.py))
  - /src/features/         -> Feature builder ([src/features/feature_builder.py](src/features/feature_builder.py))
  - /src/models/           -> Training and model artifacts ([src/models/train_baseline.py](src/models/train_baseline.py))
  - /src/normalization/    -> Canonical mappings ([src/normalization/team_aliases.py](src/normalization/team_aliases.py))
  - /src/etl/              -> Season helpers and ETL utils ([src/etl/season_utils.py](src/etl/season_utils.py))
- /data/
  - /data/raw/             -> raw downloads (e.g., football-data-co-uk CSVs)
  - /data/processed/       -> canonical matches, labels, features (parquet/csv)
  - /data/models/          -> trained model artifacts
- /models/                 -> persisted model files (JSON, joblib)
- /scripts/                -> CLI scripts for downloads/parsing/backfills ([scripts/parse_fdco_csv.py](scripts/parse_fdco_csv.py), [scripts/backfill_football_data_co_uk.py](scripts/backfill_football_data_co_uk.py))
- /tests/                  -> unit/integration tests
- /ci/                     -> CI workflows (build, lint, test, publish)
- /docs/                   -> design docs, data contracts, API spec
- PROJECT_SCHEMA.md        -> this document

Data contracts
- Canonical match record (matches_canonical.csv)
  - match_id, league, season, match_date_utc, home_team (canonical), away_team (canonical), home_goals, away_goals
- Labels (labels.csv)
  - match_id, label_OU25 (0/1), label_BTTS (0/1), other labels
- Feature batch (parquet)
  - one-row-per-match with computed features: elo_home, elo_away, rolling_goals_home_3, head2head_stats, last5_OU25_rate_home, odds_* aggregated
- Odds snapshots
  - timestamp, source, match_id, market, selection, price

Database schema (if using Postgres)
- teams (id, name_canonical, aliases JSONB)
- matches (id, league, season, date_utc, home_team_id, away_team_id, home_goals, away_goals, raw_source JSONB)
- odds_snapshots (id, match_id, timestamp_utc, source, market, selection, price, raw JSONB)
- features (match_id, computed_features JSONB, created_at)
- predictions (id, match_id, model_name, model_version, prob, metadata JSONB, created_at)

Backend API (serving)
- GET /health
- POST /predict (body: match_id or feature vector) -> { model, version, prob, meta }
- GET /models -> list available models and versions
- POST /ingest/odds -> accept odds snapshots (streaming)
- POST /ingest/match -> accept canonical match updates
- GET /backtest?model=... -> run or return backtest summaries (async)

Frontend (core pages)
- Dashboard: upcoming matches, model confidence, odds overlay
- Backtesting UI: filter by league/date, view P&L and calibration
- Explainability: feature importance, SHAP per-match
- Admin: kickoff backfill jobs, view ingestion status

CI/CD
- lint (python: flake8/ruff), unit tests, get_errors
- python package build/test
- if `/backend` exists: `npm run build` (backend); if `/frontend` exists: `npm run build` (frontend)
- gated deploy only after all builds/tests pass

Milestones & checkpoints
1. Project schema & repo layout (this doc) — complete
2. Data ingestion skeleton + normalization — DONE (scrapers, FDCO parser, team_aliases)
3. Feature builder + local training pipeline — DONE (`features_batch.parquet`, baseline model)
4. Backtesting harness + metrics storage — IN PROGRESS
5. Backend serving API (model endpoints) — PLANNED
6. Frontend dashboard (visualize predictions) — PLANNED
7. CI/CD: enforce pre-commit build checks and automated tests — PLANNED
8. Monitoring, model registry, retraining pipeline — PLANNED

Progress mapping (what exists now)
- Data pipeline & scripts: [src/features/feature_builder.py](src/features/feature_builder.py), [src/models/train_baseline.py](src/models/train_baseline.py)
- Scrapers & normalization: [src/scrapers/sofascore_scraper.py](src/scrapers/sofascore_scraper.py), [src/normalization/team_aliases.py](src/normalization/team_aliases.py)
- ETL helpers: [scripts/parse_fdco_csv.py](scripts/parse_fdco_csv.py), [scripts/backfill_football_data_co_uk.py](scripts/backfill_football_data_co_uk.py), [src/etl/season_utils.py](src/etl/season_utils.py)
- Odds parsing: [src/odds/oddsportal_parser.py](src/odds/oddsportal_parser.py), [src/odds/odds_collector.py](src/odds/odds_collector.py)
- Artifacts produced: `data/processed/features_batch.parquet`, `models/baseline_xgb.json`, `models/metrics.txt`

Immediate next steps (recommended)
1. Confirm desired backend tech (Node/Express or Python FastAPI). I recommend FastAPI if you want to keep everything Pythonic and easier to deploy with the ML artifacts.
2. Create minimal backend service scaffold (FastAPI) that exposes the `POST /predict` endpoint and a healthcheck.
3. Add CI job to run `get_errors`, python tests, and only run JS builds if `backend`/`frontend` exist.
4. Commit the schema and current Python artifacts. Because there is no `/backend` or `/frontend` yet, adjust the commit checklist to run Python checks only (or provide the real subprojects) before committing.

Repository checklist and rules (enforced)
- Always run `get_errors` before commits.
- Run both builds only if the corresponding directories exist. CI must only require builds for present subprojects.
- Never commit broken builds. If `backend` and `frontend` are not present, skip their builds locally but CI should be configured to fail if those directories are later added and builds fail.

Commands to run locally
```powershell
# Python diagnostics
python -m pip install -r requirements.txt
python -m pytest --maxfail=1 -q
# Run the parse + feature + train flow (example)
python scripts/parse_fdco_csv.py --csv data/raw/football-data-co-uk/testleague_2223.csv --outdir data/processed --league testleague
python src/features/feature_builder.py --matches data/processed/matches_canonical.csv --out data/processed/features_batch.parquet
python src/models/train_baseline.py --features data/processed/features_batch.parquet --labels data/processed/labels.csv --out models
```

Notes & decisions for you
- Choose backend tech: `FastAPI` (recommended) or Node/Express.
- Decide whether to require full `backend`/`frontend` builds before commits while the repo is still primarily Python-first.

---
Generated: mapped to current workspace state.
