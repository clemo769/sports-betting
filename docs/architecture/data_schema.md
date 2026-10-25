# Data Schema

## Scope

This is the architectural data model for the sports predictions system. The entities below are target schemas unless marked as currently represented by repository artifacts. This document does not create database tables, migrations, or a second persistence system.

## Current Record Vocabulary

Current source files use `match_date_utc` for the canonical event field and include source-specific provenance. The target domain vocabulary uses `event_time`. Adapters must preserve meaning and must not collapse event, availability, and ingestion timestamps.

## Entities

### MATCH

Target canonical match entity:

```text
match_id       canonical identity
sport          sport identifier
league         competition identifier
season         season identifier
home_team      canonical home team
away_team      canonical away team
event_time     scheduled/actual match event time
status         lifecycle/result status
```

Current representation: `data/processed/matches_canonical.csv` contains `match_id`, league, season, `match_date_utc`, teams, and goals, plus source provenance. `sport` and a general `status` field are not currently present as first-class columns.

### SOURCE_RECORD

Source-specific observation of a match or other information:

```text
source_record_id     source-record identity in the domain model
source               provider name
source_id            provider-native record identity
source_revision      provider update revision for the same source identity
match_id             canonical MATCH identity when linked
source_event_ts      event time reported by the source
source_available_ts  information availability/publication/update time
source_available_ref evidence reference for availability metadata
ingest_ts            retrieval/ingestion time
raw_reference        file, URL, or other raw-source reference
original_filename    parser audit field
parser_version       parser audit field
```

Current representation: FDCO and OpenFootball parser outputs contain most of these fields. FDCO `source_available_ts` is unknown. OpenFootball `source_available_ts` is the GitHub commit time for the exact file snapshot and `source_available_ref` is its commit SHA. That timestamp is not proof of original publication for each datum.

### FEATURE_SET

Target feature record:

```text
feature_set_id              PK, immutable generated instance
match_id                    required FK -> MATCH
prediction_cutoff           required timestamp for this feature context
feature_version             required feature definition/version identifier
feature_generation_version  required feature-builder code version
source_data_reference       required structured source snapshot/input reference
schema_version              required feature shape/contract version
feature_payload             required serialized feature values
created_at                  required generation timestamp
```

`feature_generation_version` identifies the feature-builder implementation that produced the row. `source_data_reference` is a structured audit value with this shape:

```text
source_data_reference:
  snapshot_id:       stable identifier for the source snapshot or ingestion run
  source_record_ids: set/list of SOURCE_RECORD.source_record_id values used
  content_hash:      hash of the exact input snapshot or manifest, when available
```

`snapshot_id` identifies the ingestion state, `source_record_ids` identifies the contributing records, and `content_hash` detects changed input content. This is deliberately a soft/audit-only reference at this stage: there is no persisted snapshot entity or source-record join table, so the nested identifiers are not database-enforced foreign keys. A future relational design may normalize them into a snapshot entity and join table if hard referential integrity becomes necessary. `schema_version` identifies the serialized feature shape and field contract. These fields are required for reproducibility; model compatibility remains the MODEL/FEATURE_SET `feature_version` relationship rather than a separate field.

Current representation: feature parquet contains one row per match with Elo, last-three form, match identity, teams, dates, and optionally `prediction_cutoff`. The durable IDs, code version, source reference, schema version, payload wrapper, and creation timestamp are not yet implemented.

### PREDICTION

Target historical prediction record:

```text
prediction_id
match_id
feature_set_id
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

`feature_set_id` identifies the exact generated feature instance. `feature_version` is retained as a denormalized convenience and audit field even though it is derivable through `feature_set_id`; it must agree with the referenced FEATURE_SET. `prediction_cutoff` and `predicted_at` remain separate facts. A MATCH may have many PREDICTION rows at different cutoffs.

Current representation: not implemented. The baseline trainer writes a model JSON file and metrics text, not prediction records.

### MODEL

Target model record:

```text
model_id
model_version
model_type
feature_version
training_run_id
training_data_reference
artifact_reference
configuration_reference
created_at
evaluation_status
```

`artifact_reference` points to the saved model object and is distinct from the training data reference. `configuration_reference` preserves the hyperparameters/configuration used. `training_run_id` distinguishes one execution from logical model identity/version. `evaluation_status` records whether the model has passed its evaluation gate.

Current representation: `models/baseline_xgb.json` and `models/metrics.txt` exist, but there is no model registry or durable metadata entity.

### RESULT_OBSERVATION

Target observed result record:

```text
result_observation_id
match_id
source_record_id
result_data
observed_at
observation_status
supersedes_observation_id
resolution_reason
resolved_at
resolved_by
```

`result_observation_id` makes each source-backed observation immutable. `source_record_id` preserves provenance when available. `observation_status` distinguishes `observed`, `superseded`, `disputed`, and `resolved` states. `supersedes_observation_id` links a correction to the prior observation; resolution fields explain how conflicts were handled. Corrections create new observations and do not silently overwrite history.

Current representation: goals are carried in canonical match CSV rows and are used to derive labels. A separate observed-result entity does not exist.

## Provenance and Temporal Rules

These fields are distinct:

- `source_event_ts`: when the match event occurred or is scheduled to occur.
- `source_available_ts`: when the specific source information became public, if established.
- `ingest_ts`: when this system retrieved the record.
- `prediction_cutoff`: the information boundary for one prediction.

An information item is usable only when:

```text
source_available_ts <= prediction_cutoff
```

Equality is valid. Missing availability is `unknown`, not available. Ingestion time must not be substituted for availability time. Historical result features have the separate rule:

```text
historical_event_time < current_match_event_time
```

Same-event-time results are isolated from one another. FDCO/OpenFootball currently do not provide trustworthy per-record publication timestamps, so publication-time safety cannot be claimed for those records. The OpenFootball repository commit timestamp is only a conservative snapshot update bound.

## Relationships

```text
SOURCE_RECORD
      |
      | many-to-one via match_id
      v
    MATCH
      |
      +--------------+------------------+
      v              v                  v
 FEATURE_SET     PREDICTION     RESULT_OBSERVATION
      |              |
      | soft audit   | many-to-one
      | reference    v
      +--------->  MODEL
```

`match_id` is the canonical identity connecting source observations, features, predictions, and results. A MATCH can have many SOURCE_RECORD rows, such as one FDCO row and one OpenFootball row. That expected multi-source relationship is not a duplicate. Actual source duplicates remain a source/`source_id` concern.

FEATURE_SET has a soft/audit-only traceability edge to SOURCE_RECORD through `source_data_reference.source_record_ids` and `source_data_reference.snapshot_id`. It is intentionally not a hard relational relationship yet because the schema has no source-snapshot entity or many-to-many join table. The reference must still be retained and resolvable against the source-record archive when available.

Relationship cardinalities:

```text
SOURCE_RECORD       many-to-one MATCH
FEATURE_SET         many-to-one MATCH
PREDICTION          many-to-one MATCH
PREDICTION          many-to-one FEATURE_SET
PREDICTION          many-to-one MODEL
RESULT_OBSERVATION  many-to-one MATCH
RESULT_OBSERVATION  many-to-one SOURCE_RECORD (optional)
```

When `RESULT_OBSERVATION.source_record_id` is supplied, it is intended to
belong to the same `match_id` as the observation. This is a hard relational
constraint in the target design: a composite foreign key on
`(source_record_id, match_id)` references the same pair on SOURCE_RECORD.
Observations without a retained source record remain permitted.

`feature_set_id` identifies the concrete feature instance used by a prediction. `feature_version` remains on PREDICTION as a denormalized convenience and audit field, even though it is derivable through FEATURE_SET, and must agree with the referenced feature set. A match may have many historical predictions at different cutoffs.

A PREDICTION should refer to the exact `feature_set_id`, `feature_version`, `model_version`, and `prediction_cutoff` used. This makes the historical question answerable:

> What did the system know, which model and feature version did it use, what was the prediction cutoff, and what did it predict?

## Target Persistence Notes

These are conceptual entities only. No database tables, migrations, or ORM models should be inferred from this document until a persistence design is approved. File artifacts remain the current persistence mechanism.

## Persistence Review Decision

**READY WITH CHANGES**

Before persistence is implemented, the design must include the explicit `PREDICTION.feature_set_id` relationship, immutable source-backed `RESULT_OBSERVATION` records with correction metadata, and the reproducibility fields defined for FEATURE_SET and MODEL above. These are conceptual requirements only; no tables or migrations are created by this document.

See [schema_review.md](schema_review.md) for the full review of identities, required and optional fields, immutability, correction policy, API compatibility, and database constraints.
