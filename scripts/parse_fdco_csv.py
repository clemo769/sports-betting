"""Parse football-data.co.uk CSV into canonical tables.

Produces:
- data/processed/matches_canonical.csv
- data/processed/labels.csv

Labels include O/U 2.5 and BTTS (both teams to score).
"""
import argparse
import csv
import hashlib
import json
import logging
from pathlib import Path
from collections.abc import Mapping
from typing import Any
from src.normalization.team_aliases import team_identity_evidence, to_canonical
import pandas as pd
import uuid
from datetime import datetime, timezone

from src.source_evidence import build_source_row_evidence
from src.temporal import load_venue_metadata, resolve_local_event_time, resolve_venue_timezone

# parser version for provenance
PARSER_VERSION = "parse_fdco_csv_v2"


def _column_value(row: pd.Series, *names: str) -> str | None:
    columns = {str(column).strip().casefold(): column for column in row.index}
    for name in names:
        column = columns.get(name.casefold())
        if column is not None:
            value = row.get(column)
            if value is not None and not pd.isna(value) and str(value).strip():
                return str(value).strip()
    return None


def _goal(value: Any) -> int | None:
    if value is None or pd.isna(value) or str(value).strip() == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not parsed.is_integer() or parsed < 0:
        return None
    return int(parsed)


def _local_event_time(raw_date: str | None, raw_time: str | None) -> datetime | None:
    if not raw_date or not raw_time:
        return None
    parsed = pd.to_datetime(f"{raw_date} {raw_time}", dayfirst=True, errors="coerce")
    if pd.isna(parsed):
        return None
    local_time = parsed.to_pydatetime()
    if local_time.tzinfo is not None and local_time.utcoffset() is not None:
        return None
    return local_time


def parse(
    csv_path: Path,
    outdir: Path,
    league: str = "premier-league",
    venue_metadata: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, int | str]:
    df = pd.read_csv(csv_path, dtype={"Date": "string", "Time": "string"})
    with csv_path.open("r", encoding="utf-8-sig", newline="") as source_file:
        raw_csv_rows = list(csv.reader(source_file))
    if not raw_csv_rows:
        raise RuntimeError("CSV source is empty")
    raw_columns = raw_csv_rows[0]
    raw_rows = [row for row in raw_csv_rows[1:] if row]
    if len(raw_rows) != len(df):
        raise ValueError("CSV raw-row evidence does not align with parsed source rows")
    # Normalize column names
    cols = {c: c.strip() for c in df.columns}
    df.rename(columns=cols, inplace=True)

    # Event times require both source-local Date and Time plus venue evidence.
    required = ["Date", "HomeTeam", "AwayTeam"]
    for r in required:
        if r not in df.columns:
            raise RuntimeError(f"Missing required column: {r}")

    out_matches: list[dict[str, Any]] = []
    out_labels: list[dict[str, Any]] = []
    unresolved_event_times: list[dict[str, Any]] = []
    ingest_ts = datetime.now(timezone.utc).isoformat()
    snapshot_sha256 = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    source_snapshot_id = f"fdco:{csv_path.name}:sha256:{snapshot_sha256}"
    for row_index, (_, row) in enumerate(df.iterrows()):
        match_id = str(uuid.uuid4())
        home_source_name = row.get("HomeTeam")
        away_source_name = row.get("AwayTeam")
        home_identity = team_identity_evidence(home_source_name)
        away_identity = team_identity_evidence(away_source_name)
        home = to_canonical(home_source_name)
        away = to_canonical(away_source_name)
        raw_date_value = row.get("Date")
        raw_time_value = row.get("Time") if "Time" in df.columns else None
        raw_date = None if pd.isna(raw_date_value) else str(raw_date_value)
        raw_time = None if raw_time_value is None or pd.isna(raw_time_value) else str(raw_time_value)
        source = "football-data.co.uk"
        source_id = f"{csv_path.name}:{row_index}"
        venue = dict((venue_metadata or {}).get(source_id, {}))
        venue_name = venue.get("venue_name") or _column_value(row, "venue", "ground", "stadium")
        venue_country = venue.get("venue_country") or _column_value(row, "venue_country", "country")
        venue_timezone = venue.get("venue_timezone") or _column_value(
            row, "venue_timezone", "venue time zone", "timezone"
        )
        timezone_name, timezone_basis = resolve_venue_timezone(
            league=league,
            venue_name=venue_name,
            venue_country=venue_country,
            venue_timezone=venue_timezone,
        )
        local_event_time = _local_event_time(raw_date, raw_time)
        resolution_error = None
        event_time = None
        if local_event_time is None:
            resolution_error = "missing_or_invalid_date_time"
        elif timezone_name is None:
            resolution_error = timezone_basis
        else:
            try:
                event_time = resolve_local_event_time(local_event_time, timezone_name)
            except ValueError as error:
                resolution_error = str(error)

        if event_time is None:
            unresolved_event_times.append({
                "source_id": source_id,
                "original_filename": csv_path.name,
                "source_event_date_raw": raw_date,
                "source_event_time_raw": raw_time,
                "venue_name": venue_name,
                "venue_country": venue_country,
                "venue_timezone": timezone_name,
                "resolution_error": resolution_error,
            })

        home_goals = _goal(row.get("FTHG"))
        away_goals = _goal(row.get("FTAG"))

        # provenance
        raw_reference = json.dumps({
            "provider": source,
            "source_snapshot_id": source_snapshot_id,
            "source_snapshot_sha256": snapshot_sha256,
            "original_filename": csv_path.name,
                "row_index": row_index,
            "source_event_date_raw": raw_date,
            "source_event_time_raw": raw_time,
            "venue_name": venue_name,
            "venue_country": venue_country,
            "venue_timezone": timezone_name,
            "timezone_resolution_basis": timezone_basis if event_time is not None else None,
            "event_time_resolution_error": resolution_error,
            "source_row_evidence": build_source_row_evidence(
                source=source,
                legacy_source_id=source_id,
                snapshot_id=source_snapshot_id,
                snapshot_sha256=snapshot_sha256,
                source_repository=None,
                source_file_path=None,
                filename=csv_path.name,
                row_locator={"type": "csv_data_record_index", "value": row_index},
                raw_row_content={"columns": raw_columns, "values": raw_rows[row_index]},
                availability={
                    "status": "unknown",
                    "basis": None,
                    "available_at": None,
                    "snapshot_available_at": None,
                    "provider_published_at": None,
                    "available_ref": None,
                    "interpretation": "file snapshot hash does not establish availability time",
                },
                logical_identity_unavailable_reason=(
                    "FDCO rows do not provide a stable provider-issued match identifier"
                ),
                adapter_enrichment={
                    "venue_metadata": {
                        "values": venue or None,
                        "availability_status": "unknown" if venue else "not_supplied",
                        "content_hash": None,
                    },
                    "team_identity": {
                        "home": home_identity,
                        "away": away_identity,
                    },
                },
            ),
        }, sort_keys=True)
        event_time_text = event_time.isoformat() if event_time is not None else None

        out_matches.append({
            "match_id": match_id,
            "league": league,
            "season": csv_path.stem.split("_")[-1],
            "match_date_utc": event_time_text,
            "source_event_date_raw": raw_date,
            "source_event_time_raw": raw_time,
            "venue_name": venue_name,
            "venue_country": venue_country,
            "venue_timezone": timezone_name,
            "timezone_resolution_basis": timezone_basis if event_time is not None else None,
            "event_time_resolution_error": resolution_error,
            "home_team": home,
            "away_team": away,
            "home_goals": home_goals,
            "away_goals": away_goals,
            # provenance fields
            "source": source,
            "source_id": source_id,
            "source_event_ts": event_time_text,
            "source_published_ts": None,
            "source_available_ts": None,
            "first_observed_at": None,
            "snapshot_available_at": None,
            "availability_basis": None,
            "source_available_ref": None,
            "source_snapshot_id": source_snapshot_id,
            "source_snapshot_sha256": snapshot_sha256,
            "snapshot_row_identity": json.loads(raw_reference)["source_row_evidence"]["identity"]["snapshot_scoped_row_id"],
            "ingest_ts": ingest_ts,
            "original_filename": csv_path.name,
            "parser_version": PARSER_VERSION,
            "raw_reference": raw_reference,
        })

        # Labels
        if home_goals is not None and away_goals is not None:
            total = home_goals + away_goals
            labels = {
                "O/U_2.5": 1 if total > 2.5 else 0,
                "BTTS": 1 if (home_goals > 0 and away_goals > 0) else 0,
            }
            for market, val in labels.items():
                out_labels.append({
                    "match_id": match_id,
                    "market": market,
                    "label_value": val,
                    # link label provenance to the match row
                    "source": source,
                    "source_id": source_id,
                    "ingest_ts": ingest_ts,
                })

    outdir.mkdir(parents=True, exist_ok=True)
    matches_df = pd.DataFrame(out_matches)
    labels_df = pd.DataFrame(out_labels)
    matches_path = outdir / "matches_canonical.csv"
    labels_path = outdir / "labels.csv"
    matches_df.to_csv(matches_path, index=False)
    labels_df.to_csv(labels_path, index=False)
    unresolved_path = outdir / "unresolved_event_times.csv"
    pd.DataFrame(
        unresolved_event_times,
        columns=[
            "source_id",
            "original_filename",
            "source_event_date_raw",
            "source_event_time_raw",
            "venue_name",
            "venue_country",
            "venue_timezone",
            "resolution_error",
        ],
    ).to_csv(unresolved_path, index=False)
    logging.info("Wrote %s and %s", matches_path, labels_path)
    logging.info(
        "Resolved %d of %d event times; quarantined %d unresolved rows in %s",
        len(out_matches) - len(unresolved_event_times),
        len(out_matches),
        len(unresolved_event_times),
        unresolved_path,
    )
    return {
        "matches_written": len(out_matches),
        "labels_written": len(out_labels),
        "event_times_resolved": len(out_matches) - len(unresolved_event_times),
        "event_times_unresolved": len(unresolved_event_times),
        "source_snapshot_id": source_snapshot_id,
    }


def main():
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="data/raw/football-data-co-uk/premier-league_2223.csv")
    p.add_argument("--league", default="premier-league")
    p.add_argument("--outdir", default="data/processed")
    p.add_argument("--venue-metadata", type=Path, default=None)
    args = p.parse_args()
    metadata = load_venue_metadata(args.venue_metadata) if args.venue_metadata else None
    parse(Path(args.csv), Path(args.outdir), league=args.league, venue_metadata=metadata)


if __name__ == "__main__":
    main()
