# Database Tooling Setup

## Scope

This document records the local database and migration-tooling decisions. Migrations 001 through 005 have been applied to the local development database; ORM and application integration remain separate tasks.

## Local Environment

- Database: local Windows PostgreSQL 18.6 service on port `5432`. The Compose configuration targets PostgreSQL 16 and was not used for this setup.
- Persistence: local Windows PostgreSQL data directory; the Compose named volume `sports_predictions_postgres_data` is an alternative environment, not the active server.
- Default local port: `5432`.
- Credentials: throwaway development values supplied through environment variables; `.env.example` contains placeholders only and is not a production secret.
- Connection configuration: `DATABASE_URL`, read by Alembic at command time.
- Migration tool: Alembic with explicit ordered files under `alembic/versions/`.
- ORM schema diffing and `db push` workflows are not used.

## Resolved Decisions

### UUID generation

Application code generates UUIDs before insert. This matches the current pipeline, where canonical identities and source-record identities are generated in Python, keeps offline/file-based and database ingestion behavior consistent, and avoids requiring a PostgreSQL extension before Migration 001. Database defaults may be reconsidered for database-owned identities later, but the initial policy is Python-generated UUIDs.

### Status vocabularies

The initial final vocabularies are:

```text
match.status:
  scheduled
  completed
  cancelled

model.evaluation_status:
  unevaluated
  passed
  failed
  retired
```

The current pipeline does not emit `match.status` or `evaluation_status`; no out-of-vocabulary values were found. Linker statuses such as `matched`, `unmatched`, and `ambiguous` are validation decisions, not MATCH lifecycle statuses.

### Timezone normalization

The target database persists instants as `timestamptz` only when the source timezone is known or an approved adapter has established the conversion. FDCO dates are parsed without a source timezone and must not be relabeled as UTC. OpenFootball match times are currently stored as source event strings without a declared timezone; they also must not be silently converted to UTC. OpenFootball's GitHub commit availability timestamp is explicitly UTC and may be normalized to `timestamptz`. Naive source timestamps remain explicitly unresolved until an adapter policy is approved.

### Backup and rollback

For this local pre-production phase, every future migration must provide a tested `downgrade()` path. Alembic downgrade is the interim rollback mechanism. Full backup, restore, and production recovery policy remains deferred to the later hardening phase before the database becomes the system of record.

## Validation Status

The `sports_predictions_dev` role and database were created on the local PostgreSQL 18.6 instance. Loopback host authentication uses `scram-sha-256`, and the project role successfully connects using the password configured in the Git-ignored `.env`. Migrations 001 (`0001_match_source`) through 005 (`0005_result_observation`) have been applied. The database contains all six domain tables plus `alembic_version`, with the expected constraints and indexes. Migrations 002 through 005 passed downgrade/re-upgrade checks; the database is currently at `0005_result_observation`. SQLAlchemy mappings are wired into Alembic, and `alembic check` detects no schema drift.

The local PostgreSQL service is version 18.6; `docker-compose.yml` targets PostgreSQL 16 and was not used. Keep the local `.env` credentials private and do not commit the file.

## Next Step

Use `scripts/persist_linked_matches.py` to write linker output only after supplying a verified `canonical_match_id,event_time` CSV with timezone offsets. The adapter writes matches, source provenance, and score observations in one caller-owned transaction and rejects false single-source links. Current FDCO/OpenFootball event times remain timezone-unresolved, so do not generate that mapping by assuming UTC. The CLI and persistence path have been tested with a rolled-back PostgreSQL transaction; no historical rows have been backfilled. Next, add API read services over the repositories while keeping the file pipeline as the default until backfill and cutover behavior are approved.
