"""Normalize saved odds snapshots into an odds_timeseries table.

Reads JSON files under `data/odds` (or specified folder) and extracts simple
rows: timestamp, source_file, url, bookie, market, selection, price.
"""
import argparse
import logging
from pathlib import Path
import json
import csv
from datetime import datetime


def extract_from_snapshot(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logging.exception("Failed to read %s", path)
        return []

    rows = []
    timestamp = datetime.utcfromtimestamp(path.stat().st_mtime).isoformat()
    url = data.get("url") if isinstance(data, dict) else None
    odds = data.get("odds") if isinstance(data, dict) else data
    if not isinstance(odds, list):
        return []
    for o in odds:
        # o may be dict with keys bookie/selection/price or raw_cols
        bookie = o.get("bookie") if isinstance(o, dict) else None
        selection = o.get("selection") if isinstance(o, dict) else None
        price = o.get("price") if isinstance(o, dict) else None
        rows.append({
            "timestamp": timestamp,
            "source_file": str(path),
            "url": url,
            "bookie": bookie,
            "selection": selection,
            "price": price,
        })
    return rows


def normalize(folder: Path, out_csv: Path):
    files = list(folder.glob("**/*.json"))
    all_rows = []
    for f in files:
        rs = extract_from_snapshot(f)
        all_rows.extend(rs)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline='', encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "source_file", "url", "bookie", "selection", "price"])
        writer.writeheader()
        for r in all_rows:
            writer.writerow(r)
    logging.info("Wrote %d odds rows to %s", len(all_rows), out_csv)


def main():
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--folder", default="data/odds")
    p.add_argument("--out", default="data/processed/odds_timeseries.csv")
    args = p.parse_args()
    normalize(Path(args.folder), Path(args.out))


if __name__ == "__main__":
    main()
