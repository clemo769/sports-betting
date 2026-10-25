# Target Data Schema Review

## Review Scope

This is a design review of the conceptual persistence schema. It does not create database tables, migrations, ORM models, backend code, API routes, or frontend code.

The review was grounded in the current repository implementation:

- FDCO parsing: `scripts/parse_fdco_csv.py`
- OpenFootball parsing: `scripts/fetch_openfootball.py`
- Match identity: `src/linking/match_linker.py`
- Features and cutoffs: `src/features/feature_builder.py`
- Temporal rules: `src/features/temporal_contract.py`
- Feature metadata: `src/features/feature_metadata.py`
- Baseline training: `src/models/train_baseline.py`
- Current validation suite: `tests/`

## A. Current Schema

The current proposed entities are:

- `MATCH`
- `SOURCE_RECORD`
- `FEATURE_SET`
- `PREDICTION`
- `MODEL`
- `RESULT`

They are conceptual target schemas. Current persistence remains CSV, parquet, JSON, and text artifacts.

### MATCH (Current Model)

```text
match_id
sport
league
season
home_team
away_team
event_time
status
```

Current repository mapping: `match_id`, `league`, `season`, `home_team`, `away_team`, and `match_date_utc` exist. `sport` and lifecycle `status` do not yet exist as first-class fields. Goals and source provenance are currently also carried in the canonical CSV.

### SOURCE_RECORD (Current Model)

```text
source_record_id
source
source_id
match_id
source_event_ts
source_available_ts
source_available_ref
ingest_ts
raw_reference
```

Current repository mapping: parser outputs include source, source ID, match ID, source event time, availability time where known, ingestion time, original filename, and parser version. `match_id` is nullable conceptually before linking. FDCO availability is unknown; OpenFootball availability is currently a repository snapshot update bound, not proof of original publication.

### FEATURE_SET (Current Model)

```text
feature_set_id
match_id
prediction_cutoff
feature_version
feature_payload
created_at
```

Current repository mapping: feature parquet contains match identity, event time, Elo, form, and optionally `prediction_cutoff`. Feature payload, version, and creation timestamp are not yet persisted as a durable entity.

### PREDICTION (Current Model)

```text
prediction_id
match_id
model_id
model_version
feature_version
prediction_cutoff
predicted_at
prediction
probabilities
confidence
status
```

Current repository mapping: prediction records are not implemented. The baseline trainer writes a model artifact and metrics, not historical predictions.

### MODEL (Current Model)

```text
model_id
model_version
model_type
feature_version
training_data_reference
created_at
```

Current repository mapping: a baseline XGBoost artifact and metrics file exist. There is no registry or durable model metadata record.

### RESULT

```text
match_id
result_data
observed_at
```

Current repository mapping: goals are currently fields on canonical match rows and are used to derive labels. A separate observed-result entity does not exist.

## B. Issues Found

### 1. Prediction-to-feature relationship is implicit

`PREDICTION` carries `feature_version` and `prediction_cutoff`, but does not explicitly reference the exact `FEATURE_SET` used. A feature version is not sufficient when multiple feature sets exist for the same match and cutoff.

### 2. Result identity is insufficient for source-backed observations

`RESULT` keyed only by `match_id` assumes one timeless result. Real sources can correct or revise an observed result, and multiple source records can report different states. The schema needs an observation identity and source-record reference without changing linker behavior.

### 3. Current field names need an explicit vocabulary mapping

The implementation uses `match_date_utc`; the target domain uses `event_time`. This is a naming/adaptation issue, not a reason to duplicate both fields in storage. The adapter must preserve timezone and meaning.

### 4. Source-record identity and provenance are not yet fully relational constraints

`source` + `source_id` is the current duplicate-detection identity. A future relational schema should make that pair unique for a provider and keep `match_id` nullable until linking completes. Existing parser metadata such as `original_filename` and `parser_version` should be retained as audit fields, but no new hash or job entity is required yet.

### 5. Publication-time evidence remains limited

The schema can store `source_available_ts`, but cannot make a timestamp trustworthy merely because the column exists. Missing values must remain unknown. OpenFootball's value identifies a public GitHub file snapshot update; FDCO has no source-provided availability timestamp.

No other entity is required for the current persistence foundation. Training runs, evaluation runs, teams, and availability-event histories may be useful later, but adding them now would be premature.

## C. Required Changes

### Change 1: Add an explicit feature-set reference to PREDICTION

```text
FIELD: feature_set_id
PURPOSE: identify the exact feature-set instance consumed by a prediction
WHY REQUIRED NOW: feature_version alone cannot identify the concrete cutoff-bound payload
WHAT BREAKS WITHOUT IT: historical predictions cannot be deterministically reconstructed
```

`feature_version` should remain because it identifies the feature definition. `feature_set_id` identifies the generated instance.

### Change 2: Make RESULT an observation record

```text
FIELD: result_observation_id
PURPOSE: identify one immutable observed result state
WHY REQUIRED NOW: source-backed results may be corrected or disagree across sources
WHAT BREAKS WITHOUT IT: corrections and competing observations would overwrite history
```

Add the following source reference to the result observation:

```text
FIELD: source_record_id
PURPOSE: identify the source observation supporting the result
WHY REQUIRED NOW: result provenance must be auditable
WHAT BREAKS WITHOUT IT: the origin of a result cannot be reconstructed
```

`match_id`, `result_data`, and `observed_at` remain. Corrections create a new observation; they do not silently overwrite an old one.

### Change 3: Preserve existing source audit fields

The target `SOURCE_RECORD` should retain these current implementation fields in addition to its core contract:

```text
original_filename
parser_version
```

They have a direct audit purpose and already exist in parser outputs. A source hash, retrieval-job entity, and generalized update history are not required for this milestone.

## D. Final Proposed Schema

### MATCH (Proposed)

```text
match_id                 PK, immutable canonical identity
sport                    required domain value
league                   required competition value
season                   required season value
home_team                required canonical team value
away_team                required canonical team value
event_time               required event timestamp with timezone/status semantics
status                   required lifecycle value
created_at               audit timestamp
updated_at               mutable synchronization timestamp
```

`created_at` and `updated_at` are included because a persistent canonical match needs creation/update auditability; they must not replace event or source timestamps. Current files do not yet implement these fields.

### SOURCE_RECORD (Proposed)

```text
source_record_id         PK, immutable system identity
source                   required provider name
source_id                required provider identity
source_revision          required provider update revision
match_id                 nullable FK -> MATCH until linked
source_event_ts          source-reported event timestamp
source_available_ts      nullable availability/publication/update timestamp
source_available_ref     nullable evidence reference
ingest_ts                required system retrieval timestamp
raw_reference            nullable raw file/URL reference
original_filename        nullable parser audit field
parser_version           nullable parser audit field
```

Conceptual constraint: `UNIQUE(source, source_id, source_revision)`, with `source_revision >= 1`. A later provider update creates a new revision row rather than overwriting the prior observation. `source_available_ts` is nullable and unknown is not equivalent to available.

### FEATURE_SET (Proposed)

```text
feature_set_id              PK, immutable generated instance
match_id                    required FK -> MATCH
prediction_cutoff           required timestamp for this feature context
feature_version             required definition/version identifier
feature_generation_version  required feature-builder code version
source_data_reference       required structured snapshot/input audit reference
schema_version              required feature shape/contract version
feature_payload             required serialized feature values
created_at                  required generation timestamp
```

`source_data_reference` contains a `snapshot_id`, contributing `source_record_ids`, and a `content_hash` when available. It is a soft/audit-only reference because no source-snapshot entity or join table exists yet; it is not a database-enforced foreign-key relationship. The reference is required because the same feature code and cutoff can produce different values from different source snapshots.

### PREDICTION (Proposed)

```text
prediction_id            PK, immutable historical prediction
match_id                 required FK -> MATCH
feature_set_id           required FK -> FEATURE_SET
model_id                 required FK -> MODEL
model_version            required model revision
feature_version          required feature definition/version
prediction_cutoff       required cutoff copied for query/audit convenience
predicted_at             required actual generation timestamp
prediction               required prediction value/decision
probabilities            required serialized probability output
confidence               optional confidence value
status                   required prediction lifecycle/status
```

`prediction_cutoff` and `predicted_at` are both required because an allowed information boundary and actual execution time are different facts. `feature_version` and `model_version` remain denormalized audit fields and should agree with their referenced records at creation time.

A match may have many predictions. This supports repeated predictions at T1, T2, and T3.

### MODEL (Proposed)

```text
model_id                 PK, logical model identity
model_version            required immutable model revision
model_type               required algorithm/type
feature_version          required compatible feature definition
training_data_reference  required dataset/snapshot reference
artifact_reference       required persisted artifact reference
configuration            optional serialized hyperparameters/configuration
created_at               required training/registration timestamp
evaluation_status        required lifecycle/evaluation status
```

`model_id` identifies the logical model; `model_version` identifies one immutable revision. `training_data_reference` and `artifact_reference` solve dataset and artifact reproducibility. `configuration` is optional until registry requirements are defined. No training-run entity is required now.

### RESULT_OBSERVATION

```text
result_observation_id     PK, immutable observation identity
match_id                 required FK -> MATCH
source_record_id         nullable FK -> SOURCE_RECORD
result_data              required observed result payload
observed_at              required observation timestamp
is_authoritative         required/controlled resolution flag
supersedes_observation_id nullable self-reference for corrections
```

Corrections create a new observation and may reference the superseded observation. A resolved current result can be selected without deleting historical observations. `is_authoritative` is a resolution decision, not a claim that the source timestamp is publication time.

## E. Relationship Model

```text
SOURCE_RECORD  many-to-one  MATCH
FEATURE_SET    many-to-one  MATCH
PREDICTION     many-to-one  MATCH
PREDICTION     many-to-one  FEATURE_SET
PREDICTION     many-to-one  MODEL
FEATURE_SET    soft/audit reference to SOURCE_RECORD via source_data_reference.source_record_ids
RESULT_OBSERVATION many-to-one MATCH
RESULT_OBSERVATION many-to-one SOURCE_RECORD (optional)
```

Expanded form:

```text
SOURCE_RECORD (many) --> MATCH (one)
FEATURE_SET (many) --> MATCH (one)
PREDICTION (many) --> MATCH (one)
PREDICTION (many) --> FEATURE_SET (one)
PREDICTION (many) --> MODEL (one)
RESULT_OBSERVATION (many) --> MATCH (one)
RESULT_OBSERVATION (many) --> SOURCE_RECORD (optional one source record per observation)
```

A canonical match can have one FDCO source record and one OpenFootball source record. That is a legitimate multi-source relationship, not a duplicate. Actual duplicate source records are governed by source/provider identity and source-event consistency.

## F. Temporal Model

```text
source event_time
        |
        | describes when the football event occurs
        v
prediction_cutoff
        |
        | information is usable only when source_available_ts <= cutoff
        v
predicted_at
        |
        | records when the system actually generated the prediction
        v
observed_at
        |
        | records when a result observation was recorded/observed
```

The complete timestamp vocabulary is:

- `event_time` / `source_event_ts`: scheduled or actual football event time.
- `source_available_ts`: when the specific source information became public, if evidenced.
- `ingest_ts`: when this system retrieved the record.
- `prediction_cutoff`: latest allowed information boundary.
- `predicted_at`: actual prediction-generation time.
- `observed_at`: time a result observation was recorded or observed.

Rules:

```text
source_available_ts <= prediction_cutoff
historical_event_time < current_match_event_time
```

Equality is valid for availability. Ingestion time must not be used as availability time. Missing availability remains unknown. Same-event-time result groups remain isolated for feature calculation.

## G. Reproducibility Model

The intended audit chain is:

```text
PREDICTION
  -> feature_set_id / feature_version / prediction_cutoff
  -> FEATURE_SET feature_payload / source_data_reference
  -> SOURCE_RECORD source, source_id, source_available_ts, source_available_ref
  -> MODEL model_id / model_version / artifact_reference / training_data_reference
```

This permits the system to answer what was predicted, when it was predicted, which cutoff was used, which feature instance and definition were used, and which model artifact produced it. It also exposes when source availability is unknown instead of manufacturing a claim.

## H. API Compatibility

The revised schema supports the target API without exposing storage details:

- `MATCH` supports `/matches` and `/matches/{match_id}`.
- `PREDICTION` supports `/predictions` and `/predictions/{prediction_id}` with cutoff, prediction time, probabilities, confidence, and model/feature references.
- `MODEL` supports `/models` and `/models/{model_id}`.
- `RESULT_OBSERVATION` can be summarized in match details and evaluation responses.

The API should return domain fields and stable identifiers, not pandas/parquet layouts. No API endpoints currently exist, so endpoint compatibility is conceptual only.

## I. Frontend Compatibility

The proposed domain data is sufficient for future frontend views of:

- fixtures and match details from `MATCH`;
- predictions, probabilities, confidence, cutoff, predicted time, and status from `PREDICTION`;
- model and feature version information from `MODEL` and `FEATURE_SET`;
- eventual observed outcomes from `RESULT_OBSERVATION`;
- explicit unknown provenance where publication time is unavailable.

No frontend implementation exists.

## J. Database Readiness

### Readiness Decision

Ready with changes.

The entity set and dependency direction are sufficient for a relational foundation, but two changes are required before persistence:

1. `PREDICTION.feature_set_id` must explicitly identify the concrete feature instance.
2. `RESULT` must become immutable source-backed observations with an observation identity and optional same-match source-record reference.

The eventual relational design should also enforce primary keys, foreign keys, `UNIQUE(source, source_id)`, nullable pre-link `SOURCE_RECORD.match_id`, immutable historical prediction fields, and indexes on match IDs, event times, prediction cutoffs, model versions, and source identifiers. These are conceptual constraints only.

## K. Implementation Status

At the time of this review, changes were documentation-only. Since then, local PostgreSQL setup and Alembic Migrations 001 through 005 have been completed.

- `match`, `source_record`, `feature_set`, `model`, `prediction`, and `result_observation` tables now exist in the local development database.
- No ORM models have been created.
- No backend, API, or frontend database integration has been implemented.
- No linker, ingestion, temporal feature, model, or test implementation was changed as part of the schema review.

## L. Test Status

The repository test suite remains the implementation validation command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The suite was last run after the documentation-only review and passed 43 tests, with the existing FDCO `datetime.utcnow()` deprecation warning. Rerun it when changing application code.

## Next Single Step

Implement SQLAlchemy models and a repository boundary for the migrated tables before adding database reads/writes to the pipeline.
