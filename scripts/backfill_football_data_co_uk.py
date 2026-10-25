"""Backfill using football-data.co.uk CSVs.

Usage:
  python scripts/backfill_football_data_co_uk.py --league premier-league --season 2223

This will download the CSV for the specified league and season and save it
under `data/raw/football-data-co-uk/`.
"""
import argparse
import logging
from pathlib import Path
from src.etl.season_utils import sofascore_season_to_fdco
import requests

LOG = logging.getLogger("fdco_backfill")

# Mapping friendly league names to football-data.co.uk codes
LEAGUE_MAP = {
    "premier-league": "E0",  # English Premier League
    "championship": "E1",
    "league-one": "E2",
    "serie-a": "I1",
    "la-liga": "SP1",
    "bundesliga": "D1",
    "ligue-1": "F1",
}


def build_url(season: str, league_code: str) -> str:
    # football-data.co.uk uses mmz4281/season/<code>.csv, where season is like 2223
    return f"https://www.football-data.co.uk/mmz4281/{season}/{league_code}.csv"


def download_csv(url: str, dest: Path) -> bool:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        LOG.exception("Failed to download %s", url)
        return False


def main():
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--league", default="premier-league")
    p.add_argument("--season", default="2223", help="season code like 2223 for 22/23")
    p.add_argument("--outdir", default="data/raw/football-data-co-uk")
    args = p.parse_args()

    league = args.league
    season = args.season
    # Accept a 4-digit SofaScore-style year and convert it to fdco '2223' format
    if len(season) == 4 and season.isdigit():
        season = sofascore_season_to_fdco(season)
    if league not in LEAGUE_MAP:
        LOG.error("Unknown league %s. Supported: %s", league, list(LEAGUE_MAP.keys()))
        return

    code = LEAGUE_MAP[league]
    url = build_url(season, code)
    outdir = Path(args.outdir)
    dest = outdir / f"{league}_{season}.csv"
    LOG.info("Downloading %s -> %s", url, dest)
    ok = download_csv(url, dest)
    if ok:
        LOG.info("Saved CSV to %s", dest)
    else:
        LOG.error("Download failed for %s", url)


if __name__ == "__main__":
    main()
