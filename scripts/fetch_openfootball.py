"""Fetch OpenFootball football.json data into the canonical match schema."""
import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from src.normalization.team_aliases import to_canonical

LOG = logging.getLogger("openfootball")

LEAGUE_FILES = {
    "premier-league": "en.1.json",
}


def source_snapshot_metadata(season: str, league: str) -> dict:
    """Return the public repository update timestamp for the source file.

    This is the GitHub commit time for the file snapshot, not the match event
    time and not proof of when the underlying data first became public.
    """
    path = f"{season}/{LEAGUE_FILES[league]}"
    response = requests.get(
        "https://api.github.com/repos/openfootball/football.json/commits",
        params={"path": path, "per_page": 1},
        headers={"Accept": "application/vnd.github+json", "User-Agent": "sports-predictions/0.1"},
        timeout=30,
    )
    response.raise_for_status()
    commits = response.json()
    if not commits or not commits[0].get("commit", {}).get("committer", {}).get("date"):
        raise RuntimeError(f"No public commit timestamp found for OpenFootball file {path}")
    commit = commits[0]
    return {
        "source_available_ts": commit["commit"]["committer"]["date"],
        "source_available_ref": commit.get("sha"),
    }


def build_url(season: str, league: str, ref: str | None = None) -> str:
    if league not in LEAGUE_FILES:
        raise ValueError(f"Unsupported OpenFootball league: {league}")
    revision = ref or "master"
    return f"https://raw.githubusercontent.com/openfootball/football.json/{revision}/{season}/{LEAGUE_FILES[league]}"


def fdco_season(season: str) -> str:
    start_year, end_year = season.split("-", 1)
    return f"{start_year[-2:]}{end_year[-2:]}"


def canonical_team(name):
    canonical = to_canonical(name)
    if canonical and canonical.lower().endswith(" fc"):
        canonical = to_canonical(canonical[:-3].strip())
    return canonical


def fetch_matches(season: str, league: str, outdir: Path) -> Path:
    snapshot = source_snapshot_metadata(season, league)
    url = build_url(season, league, snapshot["source_available_ref"])
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    payload = response.json()
    matches = payload.get("matches")
    if not isinstance(matches, list):
        raise RuntimeError("OpenFootball response does not contain a matches list")

    ingest_ts = datetime.now(timezone.utc).isoformat()
    rows = []
    for index, match in enumerate(matches):
        date = pd.to_datetime(match.get("date"), errors="coerce")
        if pd.isna(date):
            raise RuntimeError(f"Invalid OpenFootball match date at index {index}")
        score = match.get("score") or {}
        full_time = score.get("ft") or [None, None]
        rows.append({
            "match_id": f"openfootball-{season}-{index}",
            "league": league,
            "season": fdco_season(season),
            # Match FDCO's date-only canonical representation; the source time
            # remains available in source_event_ts without inventing a timezone.
            "match_date_utc": date.isoformat(),
            "home_team": canonical_team(match.get("team1")),
            "away_team": canonical_team(match.get("team2")),
            "home_goals": full_time[0],
            "away_goals": full_time[1],
            "source": "openfootball",
            "source_id": f"{season}/{LEAGUE_FILES[league]}:{index}",
            "source_ts": None,
            "source_event_ts": f"{match.get('date')}T{match.get('time', '00:00')}:00",
            "source_available_ts": snapshot["source_available_ts"],
            "source_available_ref": snapshot["source_available_ref"],
            "ingest_ts": ingest_ts,
            "original_filename": LEAGUE_FILES[league],
            "parser_version": "fetch_openfootball_v1",
        })

    outdir.mkdir(parents=True, exist_ok=True)
    output = outdir / f"openfootball_{league}_{season.replace('-', '')}.csv"
    pd.DataFrame(rows).to_csv(output, index=False)
    LOG.info("Wrote %s records to %s", len(rows), output)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", default="2023-24")
    parser.add_argument("--league", default="premier-league")
    parser.add_argument("--outdir", default="data/processed")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    fetch_matches(args.season, args.league, Path(args.outdir))


if __name__ == "__main__":
    main()