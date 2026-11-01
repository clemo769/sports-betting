# Sports Predictions Platform (Prototype)

Repository scaffold for the sports predictions platform prototype.

Quick start

1. Create a Python virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

1. Run the initial scrapers (example):

```bash
python scripts/run_scrapers.py --league "premier-league" --season "2023"
```

Data will be written under `data/` as raw JSON and odds timeseries.

1. Start the read-only API (PostgreSQL must be running and `.env` configured):

```powershell
.\.venv\Scripts\python.exe -m uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

The API documentation is available at `http://127.0.0.1:8000/docs`. Routes expose health, matches, predictions, and model versions. Prediction and model results are empty until corresponding records are persisted; evaluation and write endpoints are not implemented.

Register a trained XGBoost model version by opting in and supplying its stable model-family ID and explicit version labels. `DATABASE_URL` may be supplied in the environment or `.env` file:

```powershell
.\.venv\Scripts\python.exe -m src.models.train_baseline --register-model --model-id <model-family-uuid> --model-version <model-version> --feature-version <feature-version>
```

Each registered run receives a new training-run UUID. Its status remains `unevaluated`; model registration does not create an evaluation record.

Generate and persist predictions from a feature parquet file using a JSON manifest keyed by the exact canonical `match_id` values. Each entry must provide an explicit timezone-aware cutoff, feature-generation/schema versions, a source snapshot ID, and the contributing persisted source-record UUIDs. Every referenced source record must have known availability no later than the cutoff.

```json
{
  "<canonical-match-uuid>": {
    "prediction_cutoff": "2026-10-26T12:00:00Z",
    "feature_generation_version": "git:<feature-builder-commit>",
    "schema_version": "features-v1",
    "snapshot_id": "<source-snapshot-id>",
    "source_record_ids": ["<source-record-uuid>"]
  }
}
```

Run the writer with the registered model revision and an explicit probability threshold for converting the over-2.5 probability into a class decision:

```powershell
.\.venv\Scripts\python.exe scripts\persist_predictions.py --features data\processed\features_batch.parquet --manifest data\processed\prediction_manifest.json --model-id <model-family-uuid> --model-version <model-version> --decision-threshold 0.5
```

The writer stores predictions with status `generated` in one database transaction. Manifest coverage must match the feature file exactly; missing/unknown availability or a missing database reference rejects the transaction.

Notes

- This is an initial scaffold. Scrapers are best-effort and may require adjustments for site changes and ToS compliance.
