# Project Implementation Audit

**Audit date:** 2026-11-01  
**Repository:** `sports-predictions`  
**Audit baseline:** branch `main`, commit `7e1a2d8` (`Enforce append-only relational ingestion`)

## 1. Executive Summary

This repository is a **Python football-data prototype**. Its strongest implemented path is a file-based historical workflow: parse football-data.co.uk (FDCO) CSVs, normalize known team aliases, link FDCO/OpenFootball records, generate pre-match Elo and recent-form features, and train one XGBoost Over/Under 2.5 classifier. A PostgreSQL schema, migrations, ORM/repositories, a read-only FastAPI API, and opt-in database persistence CLIs are also implemented.

The system is **not an automated live prediction product**. The default pipeline remains files and manual scripts. The current local PostgreSQL database is migrated to head but all six domain tables contain zero rows. The inference/persistence path is conditional on a registered model, feature parquet, exact per-match manifest, persisted source records, and known source availability. FDCO availability is unknown, and OpenFootball’s commit timestamp is only a snapshot update bound, not proof of when individual results became available.

The most important correctness risk is temporal semantics: canonical event dates are stored as naive midnight values under the name `match_date_utc`; the feature builder does not filter historical inputs by source availability; and it rejects a prediction cutoff earlier than an event. These behaviors prevent a strong claim of valid pre-kickoff, as-of predictions. The baseline also uses its last-20% holdout for early stopping and final metrics, so its reported metrics are not an independent final test.

**Verified state:** 84 tests passed, zero failures, zero skipped tests, one `datetime.utcnow()` deprecation warning. The live Alembic revision is `0005_result_observation` (head), and `alembic check` found no schema drift. Read-only counts returned zero for `match`, `source_record`, `feature_set`, `model`, `prediction`, and `result_observation`.

## 2. Repository Structure

```text
.
├── .github/instructions/       Six Postman scripting instruction files; no workflows
├── alembic/                    Migration environment and five ordered migrations
│   └── versions/               Tables 0001 through 0005
├── data/                       Local ignored inputs, outputs, and backups
│   ├── raw/                     FDCO CSVs; SofaScore backfill directory is empty
│   └── processed/               Canonical/link outputs, labels, features, odds, reports
├── docs/architecture/          API, data, database, schema, and system design documents
├── models/                      Tracked XGBoost JSON artifact and metrics text
├── scripts/                     Downloads, parsing, linking validation, and persistence CLIs
├── src/
│   ├── api/                     FastAPI routes and Pydantic response schemas
│   ├── db/                      SQLAlchemy models, repositories, sessions, ingestion
│   ├── etl/                     Season-code helper
│   ├── features/                Feature builder, metadata, temporal rules
│   ├── linking/                 Cross-source match linker
│   ├── models/                  Baseline XGBoost trainer
│   ├── normalization/           Team alias map
│   ├── odds/                    Generic odds collector and heuristic parser
│   ├── scrapers/                Best-effort SofaScore HTML scraper
│   └── utils/                   Implied-probability helpers
├── tests/                       14 test modules; `synthetic/` is empty
├── .env.example                 Local PostgreSQL placeholders
├── .gitignore                   Ignores `.env`, `data/`, venvs, caches
├── docker-compose.yml           PostgreSQL 16 development service
├── pyproject.toml               Package metadata and incomplete runtime dependency list
├── requirements.txt             Pinned runtime/test dependency set
├── PROJECT_SCHEMA.md            Early conceptual project plan; partially stale
└── README.md                    Setup and entry-point notes
```

### Data and Artifact Inventory

Local raw data includes `premier-league_2223.csv`, `premier-league_2324.csv`, and `testleague_2223.csv`. Processed artifacts include `matches_canonical.csv`, `labels.csv`, `features_batch.parquet`, linked/multi-source parquet and CSV reports, an OpenFootball CSV, backups, and `odds_timeseries.csv`. The stored multi-source validation report records 380 FDCO records plus 380 OpenFootball records, 380 canonical IDs, 100% matched source rows, zero ambiguous/unmatched/duplicate records, and deterministic output. The current odds timeseries file contains only its header.

`data/` is ignored by Git and is not tracked; these local datasets and generated reports will not be present in a clean clone. `models/baseline_xgb.json` and `models/metrics.txt` are tracked. `.env` exists locally but is ignored; its contents were not inspected. No `backend/`, `frontend/`, `ci/`, `package.json`, or workflow directory exists.

## 3. Implemented Components

### Ingestion and Sources

| Component | Status | Verified behavior |
| --- | --- | --- |
| FDCO CSV downloader | Implemented foundation | [`scripts/backfill_football_data_co_uk.py`](../scripts/backfill_football_data_co_uk.py) downloads a league/season CSV. Its code map contains Premier League, Championship, League One, Serie A, La Liga, Bundesliga, and Ligue 1. |
| FDCO parser | Implemented | [`scripts/parse_fdco_csv.py`](../scripts/parse_fdco_csv.py) reads Date/HomeTeam/AwayTeam and optional full-time goals; writes canonical CSV and O/U 2.5 + BTTS labels. |
| OpenFootball fetcher | Implemented for one competition | [`scripts/fetch_openfootball.py`](../scripts/fetch_openfootball.py) supports only `premier-league`, pins the JSON download to a GitHub commit, and stores the commit SHA/time as source metadata. |
| SofaScore scraper | Partial/best effort | [`src/scrapers/sofascore_scraper.py`](../src/scrapers/sofascore_scraper.py) scrapes match links and embedded JSON, with retries and rate limiting. Event details are mostly returned as raw page state; no canonical parser is connected. The local backfill folder is empty. |
| Odds collection | Placeholder/partial | [`src/odds/odds_collector.py`](../src/odds/odds_collector.py) accepts a caller-supplied URL and retries requests, but its parser saves raw table columns rather than provider-specific market records. [`src/odds/oddsportal_parser.py`](../src/odds/oddsportal_parser.py) heuristically extracts bookie/selection/price, not a stable market schema. |
| Odds normalization | Partial | [`scripts/normalize_odds.py`](../scripts/normalize_odds.py) writes a CSV snapshot series; timestamp is derived from local file modification time and is naive, not an observed bookmaker publication time. |

**Actually represented:** football/soccer only. FDCO code can download seven named leagues, but the local historical set is Premier League focused. OpenFootball support is only Premier League. SofaScore takes a user-supplied slug but its behavior is not a validated production source integration. No other sport provider or usable upcoming-fixture service is implemented.

**Data quality and provenance:** the FDCO parser normalizes a manually maintained alias list and records source, source ID, parser version, file name, event timestamp, and ingestion timestamp. It correctly leaves `source_available_ts` null. The OpenFootball fetcher records a GitHub commit timestamp and SHA, which is an update bound for that file snapshot, not per-match publication evidence. Neither provider supplies a trustworthy per-row publication time for the current pipeline. Unknown availability must remain unknown; ingestion time is not a substitute.

### Canonical Data and Linking

[`src/linking/match_linker.py`](../src/linking/match_linker.py) builds keys from league, season, canonicalized home/away names, and parsed event time. It generates deterministic UUID5 canonical IDs, detects repeated `(source, source_id)` identities, supports cautious date-only fallback, and classifies matched/unmatched/ambiguous rows with a reason and confidence. It emits duplicate, ambiguous, and unmatched reports. The real FDCO/OpenFootball validation is in [`scripts/validate_multi_source.py`](../scripts/validate_multi_source.py); a legacy FDCO-only validator remains and can produce misleading single-source summaries.

Teams and competitions are stored as strings, not normalized relational team/competition entities. [`src/normalization/team_aliases.py`](../src/normalization/team_aliases.py) provides an exact case-insensitive alias dictionary and leaves unknown names unchanged. The database ingest layer currently hardcodes `sport="football"`.

### Features and Temporal Logic

[`src/features/feature_builder.py`](../src/features/feature_builder.py) produces exactly four numeric features:

- `home_elo`, `away_elo`: Elo initialized at 1500 and updated after completed matches with K=20.
- `home_form_last3`, `away_form_last3`: mean points per match over the team’s last three completed matches, across home and away appearances; missing form remains NaN in the feature file.

It sorts by event groups, records features before applying results from that timestamp group, and has tests that verify current/future result exclusion and same-time isolation. The trainer/inference path fills missing features with zero; inference preserves nulls in the stored feature payload.

There is no implementation of head-to-head, venue-specific form, player/lineup, injuries/suspensions, team-strength beyond Elo, competition context, or odds-derived model features. [`src/features/feature_metadata.py`](../src/features/feature_metadata.py) documents proposed/known feature semantics but is not a broader feature implementation.

[`src/features/temporal_contract.py`](../src/features/temporal_contract.py) provides standalone availability classification and cutoff parsing. **Important gap:** `feature_builder` does not use `source_available_ts` to decide which historical results are eligible. It uses event time only. Its `_resolve_prediction_cutoffs` also rejects a cutoff earlier than the target match event, despite the feature builder being described as pre-match. The intended cutoff/event ordering therefore needs an explicit product decision and correction before claiming valid pre-kickoff as-of features.

### Model Training and Prediction

[`src/models/train_baseline.py`](../src/models/train_baseline.py) trains one XGBoost `binary:logistic` model for `O/U_2.5`; label `1` means total goals greater than 2.5. It sorts chronologically, uses the first 80% for training and last 20% for early stopping and reported metrics, uses `eta=0.1`, `max_depth=4`, up to 100 rounds, and early stopping after 10 rounds. The selected best iteration is used for metrics and stored in registration configuration.

Current tracked metrics in [`models/metrics.txt`](../models/metrics.txt):

- Model log loss: `0.66986`
- Model Brier score: `0.23782`
- Model ROC AUC: `0.52538`
- Bookmaker-subset log loss: `0.53922`
- Bookmaker-subset ROC AUC: `0.67619`

The baseline scores are weak (ROC AUC is close to chance). Bookmaker metrics are calculated only for rows with usable prices, and the sample size is not reported, so the values are not a like-for-like comparison with the full model test subset. The same 20% holdout is used for early stopping and final reporting; there is no independent final test set, repeated backtest, cross-validation, calibration, class-imbalance analysis, hyperparameter search, SHAP/explanation, or P&L analysis.

Model registration is opt-in via the trainer’s `--register-model` flags. A caller supplies `model_id`, `model_version`, and `feature_version`; a new training-run UUID is generated. The training data reference stores local paths, not immutable file hashes/snapshots. Evaluation status remains `unevaluated`.

[`scripts/persist_predictions.py`](../scripts/persist_predictions.py) can load a registered XGBoost artifact, infer Over 2.5 probabilities, apply a required caller-supplied decision threshold, and persist feature sets/predictions in one transaction. It requires a JSON manifest keyed exactly to feature-row UUIDs, including cutoff and source record IDs with known availability. This path is implemented and unit-tested with a fake session/model, but there is no live end-to-end prediction run. The writer takes `feature_version` from the registered model rather than from the feature artifact; the feature file/manifest does not prove that the supplied feature schema/version matches the model’s training contract.

## 4. Canonical and Relational Data Model

### File-Based Structures

`matches_canonical.csv` contains match ID, league, season, `match_date_utc`, teams, goals, provider identifiers, event/availability/ingestion provenance, source filename, and parser version. `labels.csv` is long-form (`match_id`, `market`, `label_value`, provenance); it currently contains O/U 2.5 and BTTS. `features_batch.parquet` contains one row per match with identity, event date, teams, four features, and optionally a cutoff.

FDCO parser IDs are generated with UUID4 on every parse, so those raw canonical IDs are not stable across reruns. The linker produces deterministic canonical IDs from its composite key. Its event key currently uses `match_date_utc` and not a separate provider match ID.

### Database Structures

The project has five Alembic migrations and matching SQLAlchemy mappings for six tables: `match`, `source_record`, `feature_set`, `model`, `prediction`, and `result_observation`. Key design decisions include UUID identities, timezone-aware database timestamps, JSONB payloads, `UNIQUE(source, source_id, source_revision)`, composite model key `(model_id, model_version)`, prediction references to the exact feature set/model revision, and a composite same-match result/source foreign key. Application code enforces source revisions, availability cutoffs, model/feature compatibility, and append-only behavior; the database does not have immutable-table triggers or restricted roles.

The local database is at `0005_result_observation` head and schema comparison found no missing migration operations. Read-only counts found zero rows in every domain table. Thus the database schema is real and operational, but persisted application data is currently absent. `docker-compose.yml` targets PostgreSQL 16; the database documented and contacted for this audit is the local PostgreSQL instance (documented version 18.6).

## 5. Backend and API

`src/api/app.py` implements read-only FastAPI endpoints:

- `GET /health`
- `GET /matches` and `GET /matches/{match_id}`
- `GET /predictions` and `GET /predictions/{prediction_id}`
- `GET /models` and `GET /models/{model_id}`

Repositories provide filters and limit/offset pagination. The API has no write routes, evaluation endpoint, service layer, authentication, authorization, rate limiting, or background job integration. Database sessions are injected per request. Health checks `SELECT 1`; database setup failures return 503. API tests mock sessions/repositories and validate serialization/contracts, not a live network+database integration.

There is no frontend implementation or frontend source directory. Dashboard, upcoming-event views, match analysis, team stats, prediction history, explanations, data transparency UI, and model monitoring remain unimplemented.

## 6. Testing and Engineering Quality

The suite covers:

- API behavior and response contracts (`test_api.py`)
- ORM mappings and repository pagination constraints (`test_db_mapping.py`)
- source provenance and temporal availability (`test_availability_provenance.py`)
- event-date parsing, linking, multi-source determinism, and synthetic ambiguity/conflict cases
- Elo/form leakage and cutoff behavior (`test_temporal_features.py`, `test_leakage.py`)
- persistence constraints through fake sessions (`test_db_ingestion.py`, `test_prediction_persistence.py`)
- odds conversion utilities (`test_odds_conversion.py`)

**Audit test run:** `84 passed`, `0 failed`, `0 skipped`, `1 warning`. Warning: `scripts/parse_fdco_csv.py` uses deprecated `datetime.utcnow()`. Tests were run from an external temporary working directory with pytest cache/bytecode generation disabled; project datasets and tracked files were not changed.

No real database transaction tests are included in the suite; the ingestion tests use fake sessions. No model-training end-to-end test, scraping network test, API/live-DB test, complete pipeline test, frontend test, or persisted evaluation test exists. Linker integration tests use temporary inputs and stored real-data validation artifacts. `alembic check` returned “No new upgrade operations detected”; `alembic current` returned `0005_result_observation (head)`.

`requirements.txt` pins the runtime/test environment and includes `-e .`. `pyproject.toml` declares only a subset of runtime dependencies (FastAPI, Uvicorn, httpx, dotenv, SQLAlchemy), omitting packages required by major source modules such as pandas, NumPy, scikit-learn, XGBoost, requests, BeautifulSoup/lxml, PyArrow, and psycopg2. Installing from project metadata alone may therefore not produce a usable environment. There is no lint/type-check configuration, coverage configuration, CI workflow, or deployment pipeline.

## 7. Implemented, Partial, and Missing Status

| Capability | Status | Evidence / boundary |
| --- | --- | --- |
| FDCO history download and parsing | Implemented foundation | Download script, parser, local PL CSVs; no stable publication timestamp |
| OpenFootball history fetch | Implemented for EPL | Commit-pinned fetch and provenance; timestamp is a snapshot update bound |
| SofaScore collection | Partial/best effort | Heuristic HTML/embedded-state scraper; raw-only; no local backfill files |
| Odds capture | Partial/placeholder | Generic URL fetch and heuristic parsing; current normalized CSV is header-only |
| Team aliases | Partial | Manual alias dictionary; no team registry or competition canonicalization layer |
| Cross-source match linking | Implemented and validated for current sample | 380 pairs, deterministic result; conservative ambiguity/conflict reports |
| Multi-sport operation | Missing | Database has a `sport` field, but ingest hardcodes football and sources/features are soccer-specific |
| Temporal-safe Elo/form | Partial | Event-time order and same-time isolation tested; availability timestamps are not applied in feature calculation |
| Additional features | Missing | No head-to-head, player, lineup, injuries, suspensions, venue-specific form, or odds features |
| O/U 2.5 training | Implemented baseline | One chronological split and tracked model/metrics; evaluation methodology is not production-grade |
| BTTS prediction | Missing | Labels are generated, but no BTTS model/inference path exists |
| Future prediction inference | Implemented conditionally | Requires registered local XGBoost, feature parquet, manifest, DB records, verified availability, and caller threshold; not demonstrated against live rows |
| Durable model/prediction records | Implemented, unused in DB | Opt-in registration and prediction writer; database tables currently empty |
| Evaluation/backtesting persistence | Missing | No evaluation entity/table/API or formal backtest harness |
| PostgreSQL/Alembic/ORM | Implemented locally | Five migrations, six mapped tables, DB at head, no drift |
| Read-only API | Implemented | FastAPI health/matches/predictions/models; no auth/write/evaluation endpoints |
| Frontend | Missing | No frontend directory or package manifest |
| Scheduling/background processing | Missing | No scheduler, worker, queue, or automatic retraining/prediction jobs |
| CI/CD | Missing | No workflow or CI configuration; `.github` contains only Postman instruction files |

## 8. Existing Architectural Decisions

- File-based pipeline remains the default; relational writes are opt-in.
- PostgreSQL uses UUID, `timestamptz`, JSONB, explicit Alembic migrations, and Python-generated identities.
- Provider updates are append-only by `(source, source_id, source_revision)`; result corrections are new observations.
- Unknown source availability is retained as unknown; event time and ingestion time are not substituted for publication time.
- Predictions retain `feature_set_id`, model revision, feature version, cutoff, generated time, probabilities, confidence, and status.
- Immutable behavior is application-layer only; no database permission or trigger policy is installed.
- Source snapshots and feature/source joins are not normalized; source IDs in JSON are a soft audit reference.

## 9. Technical Debt and Risks

1. **High — temporal meaning and event timestamp precision.** FDCO `Date` is parsed without a timezone and the `Time` column is ignored; generated `match_date_utc` values are midnight-naive. OpenFootball preserves its time separately as a timezone-naive string but also writes date-only `match_date_utc`. Linker grouping and feature ordering therefore use dates, not verified kickoff instants. The feature builder’s cutoff check rejects cutoffs before the event, which conflicts with ordinary pre-match use.
2. **High — availability leakage risk.** The feature builder uses historical match results based on event time only; it does not apply `source_available_ts <= prediction_cutoff`. Existing training data has FDCO unknown availability. Prediction persistence’s later manifest gate cannot retroactively prove the feature calculation excluded information unavailable at the cutoff.
3. **High — no established future prediction dataset.** The DB has zero matches/source records/models/predictions/results. Required source records with known availability cannot be referenced until valid ingestion/backfill inputs exist; current FDCO data cannot satisfy that gate.
4. **High — model validation is not independent.** The same final 20% chronological holdout is used for early stopping and final metrics. The AUC is near chance; no calibration or full backtest establishes deployment fitness.
5. **Medium — feature/model version assertion is not sourced from the feature artifact.** Prediction writer assigns the registered model’s `feature_version` to the stored feature set; the parquet/manifest does not assert that version or content hash.
6. **Medium — odds support is not reliable enough for modeling.** Generic collector emits raw table columns, OddsPortal parser is heuristic and omits market identity, normalized snapshot time is local file mtime, and current odds timeseries is empty.
7. **Medium — nondeterministic parser IDs and source revisions.** FDCO parser generates UUID4 IDs each parse; deterministic linking later creates canonical UUID5 IDs. The relational layer now supports revisions, but there is no general source snapshot/ingestion-run manifest that binds file content to the parsed rows.
8. **Medium — development schema security.** API has no authentication/authorization. Local DB uses development credentials and Compose’s default local-only password; there is no production role policy, backup/restore runbook, database immutability trigger, or deployment configuration.
9. **Low — documentation drift.** `system_architecture.md`’s status table says database/API are not implemented and still reports 43 tests, despite the migrations/API and current 84-test suite. `PROJECT_SCHEMA.md` describes a planned backend/frontend and BTTS training; `schema_review.md` says ORM/API integration is absent; `database_tooling_setup.md` still lists adding API read services as the next step. These conflict with current source and the newer API/implementation-plan docs.
10. **Low — packaging and maintenance.** `pyproject.toml` dependencies are incomplete relative to `requirements.txt`; no lint/CI/coverage gate exists; broad exception handling and silent metric fallback in training can hide bookmaker comparison failures. `parse_fdco_csv.py` emits the current deprecation warning.

## 10. Dependencies Between Unfinished Components

```text
Source-specific timestamp policy + source availability evidence
                         |
                         v
Reliable canonical kickoff/event instants
                         |
                         v
Availability-aware feature generation and versioned feature artifact
                         |
                         v
Training/validation/test protocol + calibration/backtest
                         |
                         v
Verified model-feature compatibility and persisted predictions
                         |
                         v
Evaluation persistence/API + historical performance queries
                         |
                         v
Authenticated application API, scheduling/monitoring, frontend
```

A frontend or live serving endpoint depends on reliable future fixtures and reproducible inference inputs. Evaluation dashboards depend on a designed evaluation record model; the current relational schema intentionally has no evaluation table. Production predictions depend on source availability policy and operational immutability/recovery decisions.

## 11. Recommended Next Milestone

**Milestone: make the pre-match, as-of feature contract provable end to end before expanding the model or UI.** First settle kickoff timezone and cutoff ordering for each provider. Then ensure feature generation filters information by both strict prior event time and known availability at the cutoff, and carry a verifiable feature version/content hash through the parquet/manifest into the prediction writer. Add an end-to-end test for one fully sourced match path and keep unknown FDCO availability from being represented as known.

### Exact Files for This Milestone

- [`scripts/parse_fdco_csv.py`](../scripts/parse_fdco_csv.py): preserve provider date/time semantics; do not label date-only local timestamps UTC.
- [`scripts/fetch_openfootball.py`](../scripts/fetch_openfootball.py): retain source event-time meaning and snapshot availability distinction.
- [`src/features/temporal_contract.py`](../src/features/temporal_contract.py): define and validate the approved event/cutoff/availability ordering.
- [`src/features/feature_builder.py`](../src/features/feature_builder.py): apply cutoff and source-availability rules during feature construction; make any date-only behavior explicit.
- [`src/features/feature_metadata.py`](../src/features/feature_metadata.py): align declared rules with actual feature inputs.
- [`scripts/persist_predictions.py`](../scripts/persist_predictions.py): verify the feature artifact’s own feature/schema version and content provenance against the registered model and manifest.
- [`src/db/ingestion.py`](../src/db/ingestion.py): preserve the availability and feature/model cross-table checks at persistence boundaries.
- [`tests/test_temporal_features.py`](../tests/test_temporal_features.py), [`tests/test_availability_provenance.py`](../tests/test_availability_provenance.py), and [`tests/test_prediction_persistence.py`](../tests/test_prediction_persistence.py): test timezone semantics, availability filtering, version mismatch, and a complete synthetic persisted prediction flow.

This milestone does not require adding database tables. Do not start evaluation persistence, frontend, or production API authentication until the relevant schema and operational decisions are approved.

## 12. Git and Audit Integrity

At audit start, branch `main` was clean and up to date with `origin/main`. The only local/remote branches are `main` and `origin/main` (with `origin/HEAD` pointing to `origin/main`). Latest commit: `7e1a2d8 Enforce append-only relational ingestion`. `data/` and `.env` are ignored; no uncommitted changes existed before this report was created. This audit added only this report; no source code, datasets, configuration, or database rows were changed. Tests ran from an external temporary directory, and database inspection used read-only revision/count queries.
