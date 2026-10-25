# Relational Design Specification

## Scope and Status

This is a PostgreSQL-oriented relational design specification for the target persistence layer. It defines conceptual tables, types, nullability, keys, indexes, and constraints. It does **not** create tables, migrations, ORM models, or application code.

The design is based on [data_schema.md](data_schema.md) and the current implementation. Current persistence remains CSV, parquet, JSON, and text artifacts.

## Design Conventions

- `uuid` is used for generated system identities and canonical `match_id` values.
- `text` is used for provider, competition, version, status, and reference values whose controlled vocabularies are not yet implemented.
- `timestamptz` is used for all persisted instants. A source timestamp that is intentionally timezone-naive must be normalized by an approved adapter before persistence; it must not be silently assigned a timezone.
- `jsonb` is used for structured payloads and configuration where the structure is intentionally versioned outside relational columns.
- Historical facts are append-only. Corrections create new observations or versions and do not silently overwrite prediction or source provenance.
- `created_at` and `updated_at` are audit timestamps, never substitutes for event, availability, or prediction-cutoff timestamps.

## Entity Relationship Overview

```text
source_record (many) ------> match (one)
feature_set  (many) -------> match (one)
prediction   (many) -------> match (one)
prediction   (many) -------> feature_set (one)
prediction   (many) -------> model (one)
result_observation (many) -> match (one)
result_observation (many) -> source_record (zero or one)
```

`FEATURE_SET.source_data_reference` also contains soft audit traceability to source records. It is not a foreign key in this design because there is no persisted source-snapshot entity or join table yet.

## Table: match

### match columns

|Column|Type|Nullability|Key/constraint|Meaning|
|---|---|---:|---|---|
|`match_id`|`uuid`|NOT NULL|PK|Immutable canonical match identity.|
|`sport`|`text`|NOT NULL||Current value is expected to be `football`; retained for extensibility.|
|`league`|`text`|NOT NULL||Competition identifier.|
|`season`|`text`|NOT NULL||Season identifier such as `2324`.|
|`home_team`|`text`|NOT NULL||Canonical home-team name.|
|`away_team`|`text`|NOT NULL||Canonical away-team name.|
|`event_time`|`timestamptz`|NOT NULL||Scheduled or actual match event instant.|
|`status`|`text`|NOT NULL|CHECK|Lifecycle value, for example `scheduled`, `completed`, or `cancelled`.|
|`created_at`|`timestamptz`|NOT NULL||Persistence creation time.|
|`updated_at`|`timestamptz`|NOT NULL||Latest mutable synchronization time.|

### match constraints

- Primary key: `match_id`.
- Check: `home_team <> away_team`.
- Check: `status` is in an approved lifecycle vocabulary once that vocabulary is finalized.
- The current architecture does not require a global unique constraint on `(sport, league, season, home_team, away_team, event_time)` because rematches, corrections, and competitions may require identity resolution before uniqueness is certain.
- `match_id` is immutable after creation.

### match indexes

- `(sport, league, season, event_time)` for fixture and historical-range queries.
- `(event_time)` for temporal feature and evaluation scans.
- `(status, event_time)` for upcoming/completed filtering.

## Table: source_record

### source record columns

|Column|Type|Nullability|Key/constraint|Meaning|
|---|---|---:|---|---|
|`source_record_id`|`uuid`|NOT NULL|PK|Immutable system identity for one provider record.|
|`source`|`text`|NOT NULL||Provider name.|
|`source_id`|`text`|NOT NULL||Provider-native record identity.|
|`source_revision`|`integer`|NOT NULL|DEFAULT 1; UNIQUE with `source`, `source_id`|Provider update revision for the same provider identity.|
|`match_id`|`uuid`|NULL|FK -> `match.match_id`|Canonical match after linking; null before linking.|
|`source_event_ts`|`timestamptz`|NULL||Event instant reported by the source.|
|`source_available_ts`|`timestamptz`|NULL||Source-established public availability/update instant; null when unknown.|
|`source_available_ref`|`text`|NULL||Evidence reference such as an immutable snapshot or commit SHA.|
|`ingest_ts`|`timestamptz`|NOT NULL||System retrieval/ingestion instant.|
|`raw_reference`|`text`|NULL||Raw file, URL, or object reference.|
|`original_filename`|`text`|NULL||Parser audit field.|
|`parser_version`|`text`|NULL||Parser implementation version.|
|`created_at`|`timestamptz`|NOT NULL||Persistence creation time.|

### source record constraints

- Primary key: `source_record_id`.
- Unique constraint: `(source, source_id, source_revision)` prevents duplicate provider revisions while allowing append-only provider updates.
- Foreign key: nullable `match_id` references `match(match_id)`.
- Check: `source_available_ts` is not populated from `ingest_ts` by default.
- Same-row CHECK: when both are known, `source_available_ts <= ingest_ts`; exceptions require an explicit late-ingestion policy rather than silent correction. This can be a real PostgreSQL CHECK because both values are columns on the same `source_record` row.
- `source_available_ts` null means unknown, not available at all times.
- `source_revision >= 1`.
- Source records and their provenance timestamps are append-only. A later provider update creates a new source record with the same `(source, source_id)` and the next `source_revision`; it does not update the prior revision.

### source record indexes

- Unique btree `(source, source_id, source_revision)`.
- `(match_id)` for source-record lookup by canonical match.
- `(source, source_event_ts)` for provider history queries.
- `(source_available_ts)` for availability filtering where non-null.
- `(ingest_ts)` for ingestion audits.

## Table: feature_set

### feature set columns

|Column|Type|Nullability|Key/constraint|Meaning|
|---|---|---:|---|---|
|`feature_set_id`|`uuid`|NOT NULL|PK|Immutable generated feature instance.|
|`match_id`|`uuid`|NOT NULL|FK -> `match.match_id`|Match being represented.|
|`prediction_cutoff`|`timestamptz`|NOT NULL||Latest information boundary for this feature context.|
|`feature_version`|`text`|NOT NULL||Logical feature definition/version.|
|`feature_generation_version`|`text`|NOT NULL||Code version that generated the feature set.|
|`source_data_reference`|`jsonb`|NOT NULL|CHECK shape|Snapshot/run ID, contributing source-record IDs, and content hash when available.|
|`schema_version`|`text`|NOT NULL||Serialized feature contract version.|
|`feature_payload`|`jsonb`|NOT NULL||Feature values and explicit missing values.|
|`created_at`|`timestamptz`|NOT NULL||Feature-generation time.|

### `source_data_reference` Shape

```json
{
  "snapshot_id": "source-snapshot-or-ingestion-run-id",
  "source_record_ids": ["source-record-uuid-1", "source-record-uuid-2"],
  "content_hash": "sha256:..."
}
```

- `snapshot_id` is a stable source snapshot or ingestion-run reference.
- `source_record_ids` identifies contributing records.
- `content_hash` is optional when an exact content or manifest hash exists.
- This is a soft/audit-only reference. `source_record_ids` are not database-enforced foreign keys in the current design because no source snapshot or join table exists.
- No GIN index is defined for `source_data_reference.source_record_ids`. The value is deliberately an unindexed audit trail: reverse lookup is not a current persistence requirement, and indexing an unvalidated JSON array would not provide referential integrity. If reverse lookup becomes operationally necessary, introduce a normalized snapshot/join table rather than treating this JSON field as a relational edge.
- A future normalization may introduce `source_snapshot` and `feature_set_source_record` tables, but they are intentionally out of scope here.

### feature set constraints

- Primary key: `feature_set_id`.
- Foreign key: `match_id` references `match(match_id)`.
- Check: `source_data_reference` is a JSON object containing a non-empty `snapshot_id` and an array `source_record_ids`; exact JSON-schema enforcement may be implemented at the application boundary until the database standard is selected.
- Check: `prediction_cutoff` is a valid timestamp; source-result availability must be checked before the feature set is accepted.
- Feature sets are immutable after creation by application-layer convention only; no REVOKE policy or rejecting database trigger is currently specified, so the database provides no immutability guarantee yet.
- No uniqueness constraint on `(match_id, prediction_cutoff, feature_version)` is required: reruns may produce distinct instances, and `feature_set_id` preserves their identity.

### feature set indexes

- `(match_id, prediction_cutoff DESC)` for historical feature lookup.
- `(feature_version, schema_version)` for compatibility queries.
- `(created_at)` for generation audits.

## Table: model

### model columns

|Column|Type|Nullability|Key/constraint|Meaning|
|---|---|---:|---|---|
|`model_id`|`uuid`|NOT NULL|Part of composite PK|Logical model identity.|
|`model_version`|`text`|NOT NULL|UNIQUE with `model_id`|Immutable model revision.|
|`model_type`|`text`|NOT NULL||Algorithm/type, such as XGBoost.|
|`feature_version`|`text`|NOT NULL||Compatible feature definition.|
|`training_run_id`|`uuid`|NOT NULL||One training execution identity.|
|`training_data_reference`|`jsonb`|NOT NULL||Dataset/snapshot reference used for training.|
|`artifact_reference`|`text`|NOT NULL||Saved model artifact URI/path/object reference.|
|`configuration_reference`|`jsonb`|NULL||Hyperparameter/configuration reference or serialized configuration.|
|`created_at`|`timestamptz`|NOT NULL||Training/registration time.|
|`evaluation_status`|`text`|NOT NULL|CHECK|Evaluation lifecycle state.|

### model constraints

- Primary key: `(model_id, model_version)` is the relational identity of one immutable revision; `model_id` alone identifies the logical model family.
- Unique constraint: `(model_id, model_version)`.
- `training_run_id` is not interchangeable with `model_id` or `model_version`.
- `artifact_reference` must identify a saved model object and must not be inferred from `training_data_reference`.
- `evaluation_status` uses a controlled vocabulary such as `unevaluated`, `passed`, `failed`, or `retired`.
- Model revisions are immutable by application-layer convention only; no REVOKE policy or rejecting database trigger is currently specified, so the database provides no immutability guarantee yet. A retraining creates a new version and normally a new training run.

### model indexes

- `(model_id, created_at DESC)` for current/latest model lookup.
- `(feature_version, evaluation_status)` for compatible deployable models.
- `(training_run_id)` for training audit queries.

## Table: prediction

### prediction columns

|Column|Type|Nullability|Key/constraint|Meaning|
|---|---|---:|---|---|
|`prediction_id`|`uuid`|NOT NULL|PK|Immutable historical prediction identity.|
|`match_id`|`uuid`|NOT NULL|FK -> `match.match_id`|Predicted match.|
|`feature_set_id`|`uuid`|NOT NULL|FK -> `feature_set.feature_set_id`|Exact feature instance consumed.|
|`model_id`|`uuid`|NOT NULL|Logical model reference|Model family.|
|`model_version`|`text`|NOT NULL|FK pair -> `model`|Exact model revision.|
|`feature_version`|`text`|NOT NULL|Denormalized audit field|Feature definition asserted at prediction time.|
|`prediction_cutoff`|`timestamptz`|NOT NULL||Information boundary used.|
|`predicted_at`|`timestamptz`|NOT NULL||Actual generation time.|
|`prediction`|`text`|NOT NULL||Prediction value or decision.|
|`probabilities`|`jsonb`|NOT NULL||Probability output by class/market.|
|`confidence`|`numeric`|NULL|CHECK 0..1|Optional confidence.|
|`status`|`text`|NOT NULL|CHECK|Prediction lifecycle/status.|

### prediction constraints

- Primary key: `prediction_id`.
- Foreign key: `match_id` references `match(match_id)`.
- Foreign key: `(model_id, model_version)` references `model(model_id, model_version)`.
- Foreign key: `feature_set_id` references `feature_set(feature_set_id)`.
- `feature_version` must agree with the referenced FEATURE_SET at creation time; application or trigger validation should reject disagreement.
- `prediction_cutoff` and `predicted_at` are distinct and both required.
- A match may have many predictions. Do not impose uniqueness on `match_id`.
- Historical prediction fields are immutable by application-layer convention only; no REVOKE policy or rejecting database trigger is currently specified, so the database provides no immutability guarantee yet. A corrected prediction is a new prediction record with an explicit status or supersession policy.
- Check: `confidence >= 0 AND confidence <= 1` when non-null.
- `status` uses a controlled vocabulary such as `generated`, `superseded`, `invalid`, or `published`.

### prediction indexes

- `(match_id, predicted_at DESC)` for prediction history.
- `(model_id, model_version, predicted_at DESC)` for model audit.
- `(prediction_cutoff)` for temporal queries.
- `(status, predicted_at DESC)` for operational views.

## Table: result_observation

### result observation columns

|Column|Type|Nullability|Key/constraint|Meaning|
|---|---|---:|---|---|
|`result_observation_id`|`uuid`|NOT NULL|PK|Immutable observed-result identity.|
|`match_id`|`uuid`|NOT NULL|FK -> `match.match_id`|Match whose result was observed.|
|`source_record_id`|`uuid`|NULL|FK -> `source_record.source_record_id`|Supporting source record when available.|
|`result_data`|`jsonb`|NOT NULL||Observed score/result payload.|
|`observed_at`|`timestamptz`|NOT NULL||When this observation was recorded/observed.|
|`observation_status`|`text`|NOT NULL|CHECK|`observed`, `superseded`, `disputed`, or `resolved`.|
|`supersedes_observation_id`|`uuid`|NULL|Self-FK|Prior observation replaced or corrected by this one.|
|`resolution_reason`|`text`|NULL||Explanation of correction/conflict resolution.|
|`resolved_at`|`timestamptz`|NULL||When resolution occurred.|
|`resolved_by`|`text`|NULL||Resolver or process identity.|

### result observation constraints

- Primary key: `result_observation_id`.
- Foreign key: `match_id` references `match(match_id)`.
- Foreign key: nullable `source_record_id` references `source_record(source_record_id)`.
- Same-match provenance: when `source_record_id` is non-null, it must belong to the same `match_id` as the observation. Enforce this with `UNIQUE(source_record_id, match_id)` on `source_record` and a composite foreign key `(source_record_id, match_id)` from `result_observation`.
- Self foreign key: nullable `supersedes_observation_id` references `result_observation(result_observation_id)`.
- Check: an observation cannot supersede itself.
- Check: `resolved_at` and `resolved_by` are required when status is `resolved`.
- Check: `resolution_reason` is required for `superseded`, `disputed`, and `resolved` statuses.
- Result observations are immutable by application-layer convention only; no REVOKE policy or rejecting database trigger is currently specified, so the database provides no immutability guarantee yet. A correction creates a new row with `supersedes_observation_id`; the old row remains available for audit.
- No uniqueness constraint on `match_id`: multiple sources and revisions may report the same match.
- `is_authoritative` is deliberately not a column here; `observation_status = resolved` plus resolution metadata expresses resolution without confusing authority with source availability.

### result observation indexes

- `(match_id, observed_at DESC)` for current and historical result lookup.
- `(source_record_id)` for provenance lookup.
- `(observation_status, observed_at DESC)` for resolution workflows.
- `(supersedes_observation_id)` for correction chains.

## Cross-Entity Temporal Constraints

The same-row source-record constraint can be enforced by a PostgreSQL CHECK. Cross-table temporal rules must be enforced by the ingestion/feature application boundary and, where practical, validation jobs or triggers; standard PostgreSQL CHECK constraints cannot reference other tables:

```text
source_available_ts <= prediction_cutoff
historical_event_time < current_match_event_time
```

The first rule is cross-table/contextual when `prediction_cutoff` belongs to FEATURE_SET or PREDICTION, so it remains an application-layer rule. The second rule compares historical match rows while constructing features and also remains an application-layer rule. By contrast, `source_record.source_available_ts <= source_record.ingest_ts` is a same-row constraint and is suitable for a real CHECK.

Additional rules:

- Missing `source_available_ts` is unknown and cannot be treated as available.
- `ingest_ts` is retrieval time and must not be copied into `source_available_ts`.
- A source event timestamp describes the football event; it does not establish information availability.
- Results from identical event-time groups cannot become historical inputs for one another merely due to row order.
- `prediction_cutoff` belongs to the specific FEATURE_SET and PREDICTION context.

## Relational Readiness Checklist

- Primary keys: defined for every target entity.
- Foreign keys: defined for canonical match, feature, model, source-record, and result-observation references.
- Nullable fields: limited to pre-link `source_record.match_id`, unknown source availability, optional raw/evidence references, optional model configuration, optional prediction confidence, and optional result correction references.
- Historical immutability: documented for source provenance, feature sets, model revisions, predictions, and result observations, but currently application-layer-only with no database guarantee.
- Versioning: feature definition/code/schema versions and model revision/training-run identifiers are distinct.
- Uniqueness: provider identity is unique by `(source, source_id)`; canonical multi-source records are not duplicates; prediction and result observation identities permit history.
- Indexing: event time, match identity, cutoffs, availability, model versions, source identity, and correction chains are covered.
- Soft traceability: `FEATURE_SET.source_data_reference` is structured, deliberately unindexed, and audit-only; it is not FK-enforced until a source snapshot relation exists.

## Enforcement Decisions Before Migrations

**Accepted tradeoff:** Immutability is currently a convention, not a guarantee. Revisit with REVOKE/triggers before this becomes the system of record for real predictions, not just historical backtests.

|Concern|Current design decision|Database guarantee now?|
|---|---|---|
|`source_available_ts <= ingest_ts`|Same-row PostgreSQL CHECK on `source_record`, when both are non-null.|Yes, once the table exists with that CHECK.|
|`source_available_ts <= prediction_cutoff`|Application-layer validation because cutoff belongs to a feature/prediction context and may be in another table.|No ordinary CHECK; no trigger specified yet.|
|`historical_event_time < current_match_event_time`|Application-layer feature-generation rule with validation tests.|No ordinary CHECK; no trigger specified yet.|
|`feature_set` immutability|Application-layer-only convention.|No; no REVOKE or rejecting trigger specified.|
|`model` immutability|Application-layer-only convention.|No; no REVOKE or rejecting trigger specified.|
|`prediction` immutability|Application-layer-only convention; corrections are new rows.|No; no REVOKE or rejecting trigger specified.|
|`result_observation` immutability|Application-layer-only convention; corrections are new superseding rows.|No; no REVOKE or rejecting trigger specified.|
|`source_data_reference.source_record_ids` lookup|Deliberately unindexed JSON audit trail. Normalize later if reverse lookup becomes required.|No relational FK or GIN index.|

## Not Included

This specification intentionally does not define:

- database tables or migrations;
- ORM models;
- a source snapshot table or feature/source join table;
- team or alias tables;
- training-run, evaluation-run, or backtest tables;
- API routes or backend services;
- implementation of persistence.
