# Sports Predictions Platform (Prototype)

Repository scaffold for the sports predictions platform prototype.

Quick start

1. Create a Python virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

2. Run the initial scrapers (example):

```bash
python scripts/run_scrapers.py --league "premier-league" --season "2023"
```

Data will be written under `data/` as raw JSON and odds timeseries.

Notes
- This is an initial scaffold. Scrapers are best-effort and may require adjustments for site changes and ToS compliance.
