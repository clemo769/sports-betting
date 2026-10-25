"""Persist linker output after supplying verified event-time instants.

Example:
  python scripts/persist_linked_matches.py \
    --input data/processed/matches_linked.parquet \
    --event-times data/processed/verified_event_times.csv

The event-times CSV must contain canonical_match_id,event_time. Each timestamp
must include an explicit UTC offset or timezone. No source-local date is assumed
to be UTC by this command.
"""
import argparse
import os
from pathlib import Path

import pandas as pd

from src.db.ingestion import persist_linked_records
from src.db.session import session_scope


def load_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.partition("=")[2].strip().strip('"').strip("'")
    raise RuntimeError("Set DATABASE_URL or create a local .env file")


def load_verified_event_times(path: Path) -> dict[str, pd.Timestamp]:
    frame = pd.read_csv(path, dtype={"canonical_match_id": str, "event_time": str})
    required = {"canonical_match_id", "event_time"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"event-times CSV is missing columns: {', '.join(sorted(missing))}")
    if frame["canonical_match_id"].duplicated().any():
        raise ValueError("event-times CSV contains duplicate canonical_match_id values")

    event_times = {}
    for row in frame.itertuples(index=False):
        event_time = pd.Timestamp(row.event_time)
        if pd.isna(event_time) or event_time.tzinfo is None or event_time.utcoffset() is None:
            raise ValueError(
                f"event_time for {row.canonical_match_id} must include a timezone offset"
            )
        event_times[row.canonical_match_id] = event_time.to_pydatetime()
    return event_times


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Linked match parquet file")
    parser.add_argument(
        "--event-times",
        required=True,
        type=Path,
        help="CSV with canonical_match_id,event_time using explicit timezone offsets",
    )
    args = parser.parse_args()

    records = pd.read_parquet(args.input).to_dict(orient="records")
    event_times = load_verified_event_times(args.event_times)
    database_url = load_database_url()
    with session_scope(database_url) as session:
        counts = persist_linked_records(session, records, event_times)
    for name, count in counts.items():
        print(f"{name}: {count}")


if __name__ == "__main__":
    main()