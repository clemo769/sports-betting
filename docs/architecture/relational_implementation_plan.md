# Relational Implementation Plan

## Status and Scope

This document records the approved relational design and its implementation status. Alembic migrations create database tables; ORM models and application integration are still pending.

The target database is PostgreSQL. The pipeline remains file-based by default. Local PostgreSQL 18.6 is reachable on port 5432 using the project role's password-authenticated connection. Migrations 001 through 005 are applied to the local `sports_predictions_dev` database; all six domain tables exist. SQLAlchemy mappings and repositories mirror the migrated schema, and Alembic reports no schema drift. A guarded ingestion CLI writes matched fixtures, source provenance, and result observations only when given a separate verified timezone-aware event-time mapping. Its PostgreSQL transaction path was exercised and rolled back; current source dates are unresolved, so no historical rows have been backfilled. Backend API integration remains unimplemented. Docker Compose targets PostgreSQL 16 but was not used for the active local server.

## Final Decisions

- Persist all instants as `timestamptz`.
- Use `uuid` for system identities.
- Use `text` for provider, competition, season, version, status, and external reference values.
- Use `jsonb` for feature payloads, probabilities, source snapshots, training-data references, and model configuration.
- Keep `source_record.match_id` nullable until identity linking completes.
- Preserve provider updates as append-only revisions with `source_revision` and enforce identity with `UNIQUE(source, source_id, source_revision)`.
- Use a composite MODEL primary key `(model_id, model_version)`.
- Reference that composite key from PREDICTION with `(model_id, model_version)`.
- Keep `PREDICTION.feature_version` as a denormalized audit field and validate it against the referenced FEATURE_SET.
- Keep `FEATURE_SET.source_data_reference` as an unindexed, soft audit reference. Do not create a GIN index or relational join table in the first migration.
- Treat historical immutability as application-layer-only in the first database version. Revisit restricted permissions or rejecting triggers before production predictions become system-of-record data.

## Table Build Order

The migration sequence should follow dependency order:

```text
1. match
2. source_record
3. feature_set
4. model
5. prediction
6. result_observation
7. secondary indexes and validation constraints
```

The sequence permits source records to exist before linking, while all downstream records require a canonical match. Each migration must be transactional where PostgreSQL permits it and must not rewrite historical source or prediction facts silently.

## Table Specification

### `match`

```sql
match_id    uuid        NOT NULL PRIMARY KEY
sport       text        NOT NULL
league      text        NOT NULL
season      text        NOT NULL
home_team   text        NOT NULL
away_team   text        NOT NULL
event_time  timestamptz NOT NULL
status      text        NOT NULL
created_at  timestamptz NOT NULL
updated_at  timestamptz NOT NULL
```

Constraints:

```sql
CHECK (btrim(sport) <> '')
CHECK (btrim(league) <> '')
CHECK (btrim(season) <> '')
CHECK (btrim(home_team) <> '')
CHECK (btrim(away_team) <> '')
CHECK (home_team <> away_team)
CHECK (status IN ('scheduled', 'completed', 'cancelled'))
CHECK (updated_at >= created_at)
```

The status vocabulary is intentionally small for the first design. Additional lifecycle states require a documented schema revision. No global team/date uniqueness constraint is defined because canonical identity is established by the linker and rematches are valid.

Indexes:

```text
match (sport, league, season, event_time)
match (event_time)
match (status, event_time)
```

### `source_record`

```sql
source_record_id     uuid        NOT NULL PRIMARY KEY
source                text        NOT NULL
source_id             text        NOT NULL
source_revision       integer     NOT NULL DEFAULT 1
match_id              uuid        NULL REFERENCES match(match_id)
source_event_ts      timestamptz NULL
source_available_ts  timestamptz NULL
source_available_ref text        NULL
ingest_ts             timestamptz NOT NULL
raw_reference         text        NULL
original_filename     text        NULL
parser_version        text        NULL
created_at            timestamptz NOT NULL
```

Constraints:

```sql
UNIQUE (source, source_id, source_revision)
CHECK (btrim(source) <> '')
CHECK (btrim(source_id) <> '')
CHECK (source_revision >= 1)
CHECK (source_available_ts IS NULL OR source_available_ts <= ingest_ts)
CHECK (original_filename IS NULL OR btrim(original_filename) <> '')
CHECK (parser_version IS NULL OR btrim(parser_version) <> '')
```

The availability/ingestion comparison is a same-row CHECK. It is not the prediction cutoff rule. Null `source_available_ts` means unknown and is never converted from `ingest_ts` or `source_event_ts`.
`source_revision` is implicit provider-update numbering, while `supersedes_observation_id` is an explicit correction pointer: the former represents provider republication, the latter correction of a disputed result.

Indexes:

```text
UNIQUE btree (source, source_id, source_revision)
source_record (match_id)
source_record (source, source_event_ts)
source_record (source_available_ts) WHERE source_available_ts IS NOT NULL
source_record (ingest_ts)
```

### `feature_set`

```sql
feature_set_id              uuid        NOT NULL PRIMARY KEY
match_id                    uuid        NOT NULL REFERENCES match(match_id)
prediction_cutoff           timestamptz NOT NULL
feature_version             text        NOT NULL
feature_generation_version  text        NOT NULL
source_data_reference       jsonb       NOT NULL
schema_version              text        NOT NULL
feature_payload             jsonb       NOT NULL
created_at                  timestamptz NOT NULL
```

Constraints:

```sql
CHECK (btrim(feature_version) <> '')
CHECK (btrim(feature_generation_version) <> '')
CHECK (btrim(schema_version) <> '')
CHECK (jsonb_typeof(source_data_reference) = 'object')
CHECK (jsonb_typeof(source_data_reference->'snapshot_id') = 'string')
CHECK (btrim(source_data_reference->>'snapshot_id') <> '')
CHECK (jsonb_typeof(source_data_reference->'source_record_ids') = 'array')
CHECK (jsonb_typeof(feature_payload) = 'object')
```

`source_data_reference` has this documented shape:

```json
{
  "snapshot_id": "source-snapshot-or-ingestion-run-id",
  "source_record_ids": ["source-record-uuid-1"],
  "content_hash": "sha256:..."
}
```

No GIN index is created for `source_data_reference`. It is an audit-only JSON value, not an operational reverse-lookup path. No database FK can validate the nested identifiers. If reverse lookup becomes a required workflow, create a normalized source-snapshot and feature-set/source-record join design in a later migration rather than adding an index to preserve a weak relationship.

Temporal acceptance requires application validation:

```text
source_available_ts <= prediction_cutoff
historical_event_time < current_match_event_time
```

These rules cannot be implemented as ordinary CHECK constraints because they depend on other rows or tables. Feature sets are immutable by application convention only in the first database version; no REVOKE policy or rejecting trigger is included yet.

Indexes:

```text
feature_set (match_id, prediction_cutoff DESC)
feature_set (feature_version, schema_version)
feature_set (created_at)
```

### `model`

```sql
model_id                 uuid        NOT NULL
model_version            text        NOT NULL
model_type               text        NOT NULL
feature_version          text        NOT NULL
training_run_id          uuid        NOT NULL
training_data_reference  jsonb       NOT NULL
artifact_reference       text        NOT NULL
configuration_reference  jsonb       NULL
created_at               timestamptz NOT NULL
evaluation_status        text        NOT NULL
```

Key and constraints:

```sql
PRIMARY KEY (model_id, model_version)
CHECK (btrim(model_version) <> '')
CHECK (btrim(model_type) <> '')
CHECK (btrim(feature_version) <> '')
CHECK (btrim(artifact_reference) <> '')
CHECK (jsonb_typeof(training_data_reference) = 'object')
CHECK (configuration_reference IS NULL OR jsonb_typeof(configuration_reference) = 'object')
CHECK (evaluation_status IN ('unevaluated', 'passed', 'failed', 'retired'))
```

`model_id` identifies a logical model family. `(model_id, model_version)` identifies one immutable revision. `training_run_id` is separate and distinguishes the execution that produced the revision. `artifact_reference` points to the saved model object; it is not derived from `training_data_reference`.

Indexes:

```text
model (model_id, created_at DESC)
model (feature_version, evaluation_status)
model (training_run_id)
```

### `prediction`

```sql
prediction_id      uuid        NOT NULL PRIMARY KEY
match_id           uuid        NOT NULL REFERENCES match(match_id)
feature_set_id     uuid        NOT NULL REFERENCES feature_set(feature_set_id)
model_id           uuid        NOT NULL
model_version      text        NOT NULL
feature_version    text        NOT NULL
prediction_cutoff  timestamptz NOT NULL
predicted_at       timestamptz NOT NULL
prediction          text        NOT NULL
probabilities       jsonb       NOT NULL
confidence          numeric     NULL
status              text        NOT NULL
```

Foreign key and constraints:

```sql
FOREIGN KEY (model_id, model_version)
  REFERENCES model(model_id, model_version)
CHECK (btrim(feature_version) <> '')
CHECK (btrim(prediction) <> '')
CHECK (jsonb_typeof(probabilities) = 'object')
CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
CHECK (status IN ('generated', 'superseded', 'invalid', 'published'))
```

`feature_set_id` identifies the exact feature instance. `feature_version` is retained as a denormalized audit/query field and must equal the referenced feature set's version at creation time. That cross-table equality is application validation or a future trigger, not an ordinary CHECK.

`prediction_cutoff` is the allowed information boundary; `predicted_at` is when generation actually occurred. A match may have many predictions. There is no uniqueness constraint on `match_id`.

Predictions are immutable by application convention only in the first database version. Corrections create new prediction rows and do not update historical values.

Indexes:

```text
prediction (match_id, predicted_at DESC)
prediction (model_id, model_version, predicted_at DESC)
prediction (prediction_cutoff)
prediction (status, predicted_at DESC)
```

### `result_observation`

```sql
result_observation_id     uuid        NOT NULL PRIMARY KEY
match_id                  uuid        NOT NULL REFERENCES match(match_id)
source_record_id          uuid        NULL REFERENCES source_record(source_record_id)
result_data               jsonb       NOT NULL
observed_at               timestamptz NOT NULL
observation_status        text        NOT NULL
supersedes_observation_id uuid        NULL REFERENCES result_observation(result_observation_id)
resolution_reason         text        NULL
resolved_at               timestamptz NULL
resolved_by               text        NULL
```

Constraints:

```sql
CHECK (supersedes_observation_id IS NULL OR supersedes_observation_id <> result_observation_id)
CHECK (observation_status IN ('observed', 'superseded', 'disputed', 'resolved'))
CHECK (jsonb_typeof(result_data) = 'object')
CHECK (
  observation_status NOT IN ('superseded', 'disputed', 'resolved')
  OR resolution_reason IS NOT NULL AND btrim(resolution_reason) <> ''
)
CHECK (
  observation_status <> 'resolved'
  OR resolved_at IS NOT NULL AND resolved_by IS NOT NULL AND btrim(resolved_by) <> ''
)
```

When `source_record_id` is non-null, the referenced source record must have the
same `match_id` as the observation. Enforce this with a composite foreign key:

```sql
UNIQUE (source_record_id, match_id) ON source_record
FOREIGN KEY (source_record_id, match_id)
  REFERENCES source_record(source_record_id, match_id)
```

The nullable `source_record_id` permits observations without a retained source
record, but a supplied source reference is database-enforced as same-match.
The composite FK intentionally means result observations can reference only already-linked source records.

Result observations are immutable by application convention only in the first database version. Corrections create a new row with `supersedes_observation_id`; the old row remains available. There is no uniqueness constraint on `match_id` because multiple sources and revisions are expected.

Indexes:

```text
result_observation (match_id, observed_at DESC)
result_observation (source_record_id)
result_observation (observation_status, observed_at DESC)
result_observation (supersedes_observation_id)
```

## Cross-Table Enforcement Boundary

Ordinary PostgreSQL CHECK constraints can validate values on one row. They cannot safely query other tables or historical rows. Therefore:

| Rule | Enforcement in first database design |
| --- | --- |
| `source_available_ts <= ingest_ts` | Database CHECK on `source_record`. |
| `source_available_ts <= prediction_cutoff` | Application-layer validation before FEATURE_SET/PREDICTION persistence; validation job may recheck. |
| `historical_event_time < current_match_event_time` | Feature-generation application logic and temporal tests. |
| PREDICTION `feature_version` agrees with FEATURE_SET | Application-layer validation; future trigger is optional. |
| Same-time result isolation | Feature-generation application logic and tests. |
| Historical row immutability | Application-layer convention initially; no DB trigger or REVOKE yet. |

## Migration Boundaries

### Migration 001: Canonical match and source records (applied locally)

The migration creates `match` and `source_record`, including primary keys, `source_revision`, the revision-aware source identity unique constraint, nullable pre-link match FK, same-row availability CHECK, and foundational indexes. It is applied locally. Backfill only after a source-to-target mapping and timestamp policy are approved.

### Migration 002: Feature sets (applied locally)

The migration creates `feature_set` with its JSONB shape checks, match FK, and indexes. It passed a local downgrade to Migration 001 and re-upgrade to head. No source snapshot table, join table, or GIN index was added. Existing file-based features still need explicit feature/code/schema versions and a source-data reference before persistence.

### Migration 003: Models (applied locally)

The migration creates `model` with composite primary key `(model_id, model_version)`, artifact/configuration references, training-run identity, evaluation status, and model indexes. It passed a local downgrade to Migration 002 and re-upgrade to head.

### Migration 004: Predictions (applied locally)

The migration creates `prediction` with composite model FK, feature-set FK, match FK, probability/confidence checks, and historical indexes. It passed a local downgrade to Migration 003 and re-upgrade to head. The application must validate cutoff, feature version, and feature-set compatibility before insert.

### Migration 005: Result observations (applied locally)

The migration creates `result_observation` with source and self-referential FKs, including the composite same-match source-record FK, correction checks, resolution checks, and correction-chain indexes. It passed a local downgrade to Migration 004 and re-upgrade to head.

### Later hardening migration

Before the database becomes the system of record for real predictions, separately decide and implement one or both of:

- restricted database roles with `REVOKE UPDATE, DELETE` on immutable historical tables;
- rejecting triggers that permit only approved append-only operations.

That hardening migration must include an operational recovery procedure and tests. It is intentionally not part of this design-only milestone.

## Implementation Acceptance Checklist

- [ ] Confirm PostgreSQL version and migration tool.
- [ ] Confirm UUID generation policy.
- [ ] Confirm canonical team/competition status vocabularies.
- [ ] Confirm whether all persisted source timestamps can be normalized to `timestamptz` without inventing timezone information.
- [ ] Define the source snapshot/run identifier format used in `source_data_reference`.
- [ ] Define feature, schema, and model version naming conventions.
- [ ] Define training-run identifier ownership before inserting MODEL rows.
- [ ] Define application validation tests for cross-table temporal rules.
- [ ] Approve the later immutability hardening approach before real prediction persistence.
- [ ] Produce a rollback and backup plan before any migration is executed.

## Explicitly Not Implemented

This plan does not create:

- database tables;
- migration files;
- database users or permissions;
- triggers;
- ORM models;
- backend services;
- API routes;
- source snapshot or join tables;
- team/alias tables;
- training-run or evaluation-run tables.
