# Copilot Development Protocol

Copilot is the primary developer of this project. Credit efficiency means less waste, never less capability: implement fully, test properly, and keep engineering standards. Where this protocol conflicts with correctness, correctness wins.

Scoped files in `.github/instructions/` (Postman, via `applyTo`) apply only to their matching files and add to this protocol without conflict.

## 1. Scope
- Work within the assigned task. Identify the relevant files, modules and dependencies first and avoid unrelated directories.
- No unrelated features, refactors or speculative improvements.
- Widen scope when technically required (callers, schema, migrations, tests), and state the reason.
- Never drop necessary implementation work to save usage.

## 2. Exploration
- Read the relevant instructions and task-specific docs before starting.
- Use targeted search (symbol, filename, exact string) before recursive or semantic search. Read whole relevant ranges rather than many tiny reads.
- Do not re-read unchanged files or re-verify findings already established in the session.
- Do not produce large repository summaries. Report only what the task needs.
- Investigate dependencies whenever they affect correctness.

## 3. Tools and agents
- Use subagents or parallel investigations only when they add information the main thread cannot get cheaply.
- Do not repeat tool calls or commands that cannot return new information.
- When a command fails, diagnose the cause (read the error, inspect inputs) before rerunning. Change something between attempts.
- Keep investigating while it is useful. Tool cost is not a reason to stop early.

## 4. Implementation
- Deliver the complete requested functionality. No stubs or half-finished work to save tokens.
- Prefer minimal, reviewable changes that reuse existing abstractions. Add dependencies only with justification.
- Source code, schemas, migrations, APIs, frontend, tests and infrastructure are all in scope when the task requires them.
- No comments that restate code. No new documentation files unless requested.

## 5. Models and context
- Use an economical model for routine edits, lookups and mechanical changes.
- Use a more capable model for architecture, difficult debugging and consequential decisions (schema, temporal logic, integrity).
- Choose the model deliberately. Automatic selection is not always the cheapest.
- Keep context lean: avoid re-sending large files or pasted output when a path or line range suffices.

## 6. Testing
- Run focused tests for the affected components while developing.
- Run the broader suite (and PostgreSQL integration tests when persistence, queries or migrations changed) before finishing significant changes.
- Investigate regressions. Never weaken, delete or skip tests to get green.
- Never skip essential validation to save credits.

## 7. Completion
Each task ends when: the requested functionality is implemented, necessary tests ran, failures are fixed or explicitly documented, changes are verified, and any remaining work needs human clarification or authorization. Then stop. Do not hunt for unrelated improvements.

## 8. Project invariants (take precedence over convenience)
- Database integrity, and append-only ingestion: persisted history is never rewritten; changed provider data requires a new `source_revision`.
- Source provenance and traceability; canonical entity and match linking.
- Temporal correctness: no event-time or availability leakage. Strict pre-match and retrospective modes stay distinct, and ingestion time is never availability.
- Deterministic feature generation and reproducible predictions.
- Training and evaluation integrity.
- Security and input validation at system boundaries.
- Regression and integration testing.

## 9. Environment safety
- Implement required schema changes through Alembic migration files. Apply migrations or perform other database writes only when explicitly included in the task's authorized scope; never infer permission to mutate a shared or production database.
- Integration tests use only `TEST_DATABASE_URL` (the `sports_predictions_test` database) and must verify the database identity before writing. Never use `DATABASE_URL` for tests.
- Never print or expose `.env` contents or connection strings.
- Use the project virtual environment (`.venv`). Do not install packages globally.
- Preserve existing uncommitted work. Do not stage, commit, push, reset, stash or clean unless asked.

## 10. Task report
End each assignment with a concise report: objective, implementation, files created or modified, tests run with results, outstanding issues or dependencies, next steps. Keep it short.
