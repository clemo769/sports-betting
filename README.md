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

Notes

- This is an initial scaffold. Scrapers are best-effort and may require adjustments for site changes and ToS compliance.
