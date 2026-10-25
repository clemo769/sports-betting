# Sports Predictions System Architecture

## Purpose

This document is the versioned architectural reference for the sports predictions prototype. It describes the current Python pipeline and the intended future application layers. Target components are explicitly marked; documentation of a component does not mean that component is implemented.

## End-to-End Shape

```text
Source Data
    |
    v
Ingestion
    |
    v
Provenance
    |
    v
Canonical Data / Match Identity
    |
    v
Temporal Safety
    |
    v
Feature Engineering
    |
    v
Model System
    |
    v
Evaluation
    |
    v
Persistence / Database
    |
    v
Backend Services
    |
    v
API
    |
    v
Frontend
```

The repository currently implements the source-to-model portion as scripts and Python modules. Persistence, application services, and user interfaces are target architecture only.

## Layer Responsibilities

### Source Data

- Purpose: provide external match, result, and future information inputs.
- Responsibilities: identify source, preserve raw references, and expose source-specific semantics.
- Inputs: external provider data.
- Outputs: raw files or provider responses.
- Dependencies: external providers and network access where applicable.
- Must not: silently convert event, publication, and retrieval times into one timestamp.
- Status: partially implemented. FDCO and OpenFootball historical data are present; SofaScore and odds code is best-effort and not a complete production ingestion path.

### Ingestion

- Purpose: retrieve and parse source records into project records.
- Responsibilities: parse source formats, normalize team names, retain source identifiers, and write canonical-schema files.
- Inputs: FDCO CSV files and OpenFootball JSON files.
- Outputs: `matches_canonical.csv`, `labels.csv`, and source-specific processed CSVs.
- Dependencies: `scripts/parse_fdco_csv.py`, `scripts/fetch_openfootball.py`, normalization helpers.
- Must not: invent publication timestamps or overwrite source identity.
- Status: implemented foundation; the real multi-source baseline has 380 FDCO and 380 OpenFootball records.

### Provenance

- Purpose: explain where a record came from and when each timestamp means.
- Responsibilities: preserve `source`, `source_id`, `source_event_ts`, `source_available_ts`, and `ingest_ts`.
- Inputs: parser outputs and source metadata.
- Outputs: provenance-bearing source records.
- Dependencies: parser implementations and `src/features/temporal_contract.py`.
- Must not: set `source_available_ts = ingest_ts` without source evidence.
- Status: partial. OpenFootball records carry the exact GitHub file snapshot commit time and SHA; FDCO availability remains unknown.

### Canonical Data / Match Identity

- Purpose: connect source records representing the same match.
- Responsibilities: canonical team names, competition/season keys, deterministic IDs, duplicate and ambiguity decisions.
- Inputs: source records.
- Outputs: linked records and linker reports.
- Dependencies: `src/linking/match_linker.py` and `src/normalization/team_aliases.py`.
- Must not: loosen matching rules to improve rates or hide ambiguous records.
- Status: implemented and validated. Current baseline: 380 canonical matches, 380 legitimate multi-source relationships, zero actual duplicates, and deterministic PASS.

### Temporal Safety

- Purpose: prevent information unavailable at prediction time from entering features.
- Responsibilities: enforce event-time ordering, prediction cutoffs, same-time isolation, and explicit availability semantics.
- Inputs: event timestamps, prediction cutoffs, and availability metadata.
- Outputs: time-safe feature inputs and validation decisions.
- Dependencies: `src/features/feature_builder.py` and `src/features/temporal_contract.py`.
- Must not: use ingestion time as publication time or let a match result affect its own pre-match features.
- Status: implemented and validated. Global and per-match cutoffs, strict prior event groups, and temporal tests exist.

### Feature Engineering

- Purpose: calculate pre-match variables from valid historical information.
- Responsibilities: currently calculate pre-match Elo and last-three-match points form.
- Inputs: canonical match records and per-match cutoffs.
- Outputs: feature parquet files.
- Dependencies: `src/features/feature_builder.py` and feature metadata.
- Must not: silently repair bad provenance or compensate for temporal leakage.
- Status: partial. Current features are limited; broader documented features are not implemented.

### Model System

- Purpose: train and apply predictive models using versioned, time-safe features.
- Responsibilities: currently train a baseline XGBoost O/U 2.5 model.
- Inputs: feature parquet and labels.
- Outputs: model JSON and metrics text.
- Dependencies: `src/models/train_baseline.py`, XGBoost, scikit-learn.
- Must not: compensate for leakage or change feature semantics.
- Status: partial. A baseline exists; production model versioning and serving do not.

### Evaluation

- Purpose: measure predictive quality and operational usefulness.
- Responsibilities: currently calculate baseline log loss, Brier score, ROC AUC, and bookmaker comparisons where data permits.
- Inputs: predictions, labels, and optional market data.
- Outputs: metrics reports.
- Dependencies: training script and generated artifacts.
- Must not: be treated as a complete backtest or proof of deployment readiness.
- Status: partial. Complete historical backtesting, calibration, P&L, and stored evaluation runs are not implemented.

### Persistence / Database

- Purpose: store canonical domain records, feature sets, predictions, models, and evaluations durably.
- Responsibilities: future database schema, versioning, and queryable history.
- Inputs: validated pipeline outputs.
- Outputs: persistent domain records.
- Dependencies: future database choice and migrations.
- Must not: become an undocumented second source of truth for feature logic.
- Status: relational schema implemented locally through Alembic Migrations 001-005 (`match`, `source_record`, `feature_set`, `model`, `prediction`, `result_observation`), with matching SQLAlchemy mappings, repositories, and a guarded linked-record persistence CLI. The regular pipeline still persists to CSV, parquet, JSON, and text; historical database backfill is gated on verified timezone-aware event times.

### Backend Services

- Purpose: expose domain operations and orchestrate validated pipeline outputs.
- Responsibilities: future match, prediction, model, and evaluation services.
- Inputs: persisted domain entities.
- Outputs: service responses for the API.
- Dependencies: future persistence layer and application framework.
- Must not: contain the core pandas feature-engineering implementation.
- Status: read-only FastAPI routes for health, matches, predictions, and model metadata are implemented over SQLAlchemy repositories. Domain write orchestration and evaluation endpoints remain unimplemented.

Target structure:

```text
backend/
├── app
├── api/
│   ├── health
│   ├── matches
│   ├── predictions
│   ├── models
│   └── evaluation
├── services/
│   ├── match_service
│   ├── prediction_service
│   ├── model_service
│   └── evaluation_service
├── schemas/
│   ├── match
│   ├── prediction
│   ├── model
│   └── evaluation
└── tests/
```

### API

- Purpose: provide stable external contracts for clients.
- Responsibilities: health, matches, predictions, models, and evaluation reads.
- Inputs: validated API requests.
- Outputs: serialized domain responses.
- Dependencies: future backend services.
- Must not: expose internal pandas/parquet layouts as its domain contract.
- Status: read-only health, match, prediction, and model endpoints are implemented in `src/api/app.py`. Evaluation and write endpoints remain unimplemented. See [api_schema.md](api_schema.md) for contracts.

### Frontend

- Purpose: present fixtures, predictions, explanations, performance, and system status.
- Responsibilities: user interaction and visualization through the backend API.
- Inputs: API responses.
- Outputs: user-facing views.
- Dependencies: future API and frontend application.
- Must not: directly consume pandas, parquet files, training scripts, raw source files, or internal feature-builder functions.
- Status: not implemented.

Intended dependency direction:

```text
Frontend -> Backend API -> Services -> Domain/Data/Model layers
```

## Temporal Architecture

The system distinguishes four timestamps:

- `event_time`: when a match is scheduled or occurs. In source records this is represented by `source_event_ts` or the canonical event field.
- `prediction_cutoff`: the latest timestamp at which information may be used for one prediction row.
- `source_available_ts`: when the specific source information became publicly available, when the source can establish it.
- `ingest_ts`: when this system retrieved or ingested the record.

The availability rule is:

```text
source_available_ts <= prediction_cutoff
```

Equality is valid. Historical match-result features have the separate stricter rule:

```text
historical_event_time < current_match_event_time
```

`ingest_ts` must not automatically be interpreted as `source_available_ts`. The current FDCO/OpenFootball data does not provide trustworthy per-record publication timestamps. OpenFootball has a public repository snapshot update timestamp, which is an update bound for the exact file snapshot, not proof of original publication of each match datum.

## Testing Architecture

Current tests are flat under `tests/` and cover dates, normalization, linking, provenance, temporal features, odds utilities, and diagnostics. The validated suite currently passes 43 tests.

Target organization:

```text
tests/
├── ingestion/
├── linking/
├── temporal/
├── features/
├── models/
├── evaluation/
├── backend/
├── api/
└── integration/
```

- Unit tests: one function or module in isolation.
- Contract tests: guarantees across architectural boundaries, especially identity and temporal rules.
- Integration tests: multiple pipeline components working together.
- End-to-end tests: eventually Source -> Ingestion -> Canonical Match -> Features -> Model -> Backend -> API -> Frontend.

No fake backend or API tests should be added before those layers exist.

## Validation Pipeline

The current project-level validation command is:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The intended future progression is:

```text
Ingestion tests
      |
Linking tests
      |
Temporal tests
      |
Feature tests
      |
Model tests
      |
Evaluation tests
      |
Backend tests
      |
API tests
      |
Integration tests
```

A future `scripts/test_all` command may formalize this when more layers exist. It must not pretend absent layers have tests.

## Status Table

| Component | Status | Evidence |
| --- | --- | --- |
| Source ingestion | Implemented foundation / validated | FDCO and OpenFootball parsers and real data |
| Provenance | Partial / validated foundation | Provenance fields; OpenFootball snapshot timestamp; FDCO availability unknown |
| Canonical linking | Implemented / validated | `src/linking/match_linker.py`, multi-source validator |
| Event-time safety | Implemented / validated | Grouped Elo/form and temporal tests |
| Prediction cutoff | Implemented / validated | Global and per-match cutoff API |
| Per-match cutoff | Implemented / validated | `prediction_cutoffs` keyed by `match_id` |
| Availability provenance | Partial | OpenFootball file snapshot update bound only |
| Feature engineering | Partial | Elo and last-three form only |
| Model training | Partial | Baseline XGBoost trainer |
| Evaluation/backtesting | Not complete | No complete backtesting harness |
| Database | Not implemented | No database or migrations |
| Backend | Not implemented | No backend directory |
| API | Not implemented | No API routes |
| Frontend | Not implemented | No frontend directory |
| End-to-end tests | Not implemented | No full application path |
| CI/CD | Not implemented | No CI directory or workflow |

## Development Dependency Order

```text
Data
  |
Identity
  |
Temporal correctness
  |
Feature correctness
  |
Model correctness
  |
Evaluation correctness
  |
Persistence
  |
Backend
  |
API
  |
Frontend
  |
End-to-end system
```

This is a dependency order, not necessarily a rigid timeline.

## Architectural Principles

```text
Explicit > implicit
Validated > assumed
Timestamped > inferred
Reproducible > convenient
Versioned > overwritten
Isolated > coupled
Testable > clever
```

The system must prefer `insufficient information` or `unknown` over inventing missing information. False confidence is an architectural failure.

## Future Copilot Work Contract

Every future implementation task should identify:

```text
ARCHITECTURAL LAYER:
COMPONENT:
CURRENT STATUS:
DEPENDENCIES:
OBJECTIVE:
IN SCOPE:
OUT OF SCOPE:
TEST REQUIREMENTS:
ACCEPTANCE CRITERIA:
NEXT SINGLE STEP:
```

Copilot must not automatically implement the next milestone after completing a task.
