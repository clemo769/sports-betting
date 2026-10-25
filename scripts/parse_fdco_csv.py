"""Parse football-data.co.uk CSV into canonical tables.

Produces:
- data/processed/matches_canonical.csv
- data/processed/labels.csv

Labels include O/U 2.5 and BTTS (both teams to score).
"""
import argparse
import logging
from pathlib import Path
from src.normalization.team_aliases import to_canonical
import pandas as pd
import uuid
from datetime import datetime

# parser version for provenance
PARSER_VERSION = "parse_fdco_csv_v1"


def parse(csv_path: Path, outdir: Path, league: str = "premier-league"):
    df = pd.read_csv(csv_path)
    # Normalize column names
    cols = {c: c.strip() for c in df.columns}
    df.rename(columns=cols, inplace=True)

    # Expected columns: Date, HomeTeam, AwayTeam, FTHG, FTAG
    required = ["Date", "HomeTeam", "AwayTeam"]
    for r in required:
        if r not in df.columns:
            raise RuntimeError(f"Missing required column: {r}")

    # Parse Date
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")

    out_matches = []
    out_labels = []
    ingest_ts = datetime.utcnow().isoformat()
    for idx, row in df.iterrows():
        match_id = str(uuid.uuid4())
        home = to_canonical(row.get("HomeTeam"))
        away = to_canonical(row.get("AwayTeam"))
        date = row.get("Date")
        home_goals = int(row.get("FTHG")) if "FTHG" in row and not pd.isna(row.get("FTHG")) else None
        away_goals = int(row.get("FTAG")) if "FTAG" in row and not pd.isna(row.get("FTAG")) else None

        # provenance
        source = "football-data.co.uk"
        source_id = f"{csv_path.name}:{idx}"
        # FDCO does not provide a published timestamp for the row; do not fabricate
        source_ts = None
        # however the match date is available; record as source_event_ts for auditing
        source_event_ts = date.isoformat() if pd.notna(date) else None
        original_filename = csv_path.name

        out_matches.append({
            "match_id": match_id,
            "league": league,
            "season": csv_path.stem.split("_")[-1],
            "match_date_utc": date.isoformat() if pd.notna(date) else None,
            "home_team": home,
            "away_team": away,
            "home_goals": home_goals,
            "away_goals": away_goals,
            # provenance fields
            "source": source,
            "source_id": source_id,
            "source_ts": source_ts,
            "source_event_ts": source_event_ts,
            # FDCO publishes no row-level publication/update timestamp.
            "source_available_ts": None,
            "ingest_ts": ingest_ts,
            "original_filename": original_filename,
            "parser_version": PARSER_VERSION,
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
    logging.info("Wrote %s and %s", matches_path, labels_path)


def main():
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="data/raw/football-data-co-uk/premier-league_2223.csv")
    p.add_argument("--league", default="premier-league")
    p.add_argument("--outdir", default="data/processed")
    args = p.parse_args()
    parse(Path(args.csv), Path(args.outdir), league=args.league)


if __name__ == "__main__":
    main()
