# API Schema

## Scope and Status

This document defines the API contracts. Read-only FastAPI routes for health, matches, predictions, and models are implemented over SQLAlchemy repositories. No HTTP write routes are exposed. Evaluation remains unimplemented because no evaluation-result table exists.

The frontend must consume these contracts through the backend. It must not directly consume pandas dataframes, parquet files, training scripts, raw source files, or internal feature-builder functions.

## Endpoints

### `GET /health`

- Purpose: report service health and dependency status.
- Request: no body; optional operational query parameters.
- Response concept: service status, version, and dependency checks.
- Entity: service status.
- Implemented: yes.

### `GET /matches`

- Purpose: list canonical matches with filtering and pagination.
- Request: optional league, season, event-time range, status, page, and page size.
- Response concept: paginated MATCH summaries.
- Entity: MATCH.
- Implemented: yes.

### `GET /matches/{match_id}`

- Purpose: retrieve one canonical match and its permitted source/result summary.
- Request: canonical `match_id` path parameter.
- Response concept: MATCH detail, source-record references, event time, and result when available.
- Entity: MATCH and SOURCE_RECORD.
- Implemented: yes.

### `GET /predictions`

- Purpose: list historical predictions with filters.
- Request: optional match, model, feature version, event-time range, status, page, and page size.
- Response concept: paginated PREDICTION records including cutoff and model references.
- Entity: PREDICTION.
- Implemented: yes.

### `GET /predictions/{prediction_id}`

- Purpose: retrieve one historical prediction and its provenance.
- Request: `prediction_id` path parameter.
- Response concept: prediction, probabilities, confidence, cutoff, model version, feature version, and match reference.
- Entity: PREDICTION.
- Implemented: yes.

### `GET /models`

- Purpose: list registered model versions.
- Request: optional model type, status, and pagination filters.
- Response concept: MODEL summaries.
- Entity: MODEL.
- Implemented: yes.

### `GET /models/{model_id}`

- Purpose: retrieve model metadata and artifact references.
- Request: `model_id` path parameter.
- Response concept: model type, version, feature version, training-data reference, and lifecycle status.
- Entity: MODEL.
- Implemented: yes; returns all versions for the model ID, optionally filtered by `model_version`.

### `GET /evaluation`

- Purpose: retrieve evaluation and backtest summaries.
- Request: optional model, feature version, league, season, event-time range, and evaluation-run filters.
- Response concept: metric summaries with dataset and cutoff semantics.
- Entity: evaluation result, target schema not yet implemented.
- Implemented: no; evaluation persistence is not modeled yet.

## Contract Rules

- `match_id` is the canonical identity for match-related resources.
- Historical PREDICTION records preserve `model_id`, `model_version`, `feature_version`, `prediction_cutoff`, and `predicted_at`.
- API serialization must expose domain contracts, not internal CSV/parquet column layouts.
- API services must reject or represent unknown provenance explicitly rather than inventing timestamps.
- `source_available_ts <= prediction_cutoff` is the availability rule where availability is known.
- Historical result-derived features still require `historical_event_time < current_match_event_time`.
- The API must not move core feature-engineering logic into route handlers.

## Backend Boundary

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

Implemented in `src/api/app.py` with FastAPI. Database sessions are injected per request; local `.env` settings are loaded with `python-dotenv`. The API is read-only. The directory layout above is a conceptual separation rather than the current file structure.

## Frontend Boundary

```text
Frontend
    |
    v
Backend API
    |
    v
Services
    |
    v
Domain / Data / Model layers
```

The eventual frontend is expected to support fixtures, match details, predictions, prediction explanations, historical performance, and system status. It is not implemented.
