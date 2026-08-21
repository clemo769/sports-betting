"""Simple runner to execute initial scrapers (SofaScore + odds).

Usage examples:
  python scripts/run_scrapers.py --league "premier-league" --season "2023"
  python scripts/run_scrapers.py --odds-url "https://www.example.com/odds" --odds-out data/odds/sample.json
"""
import argparse
import logging
from pathlib import Path

from src.scrapers.sofascore_scraper import SofaScoreScraper
from src.odds.odds_collector import OddsCollector


def run_sofascore(league: str, season: str, outdir: str):
    s = SofaScoreScraper(rate_limit_sec=1.0)
    matches = s.fetch_match_list(league, season)
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    for i, m in enumerate(matches):
        path = out / f"match_{i}.json"
        s.save_raw(m, path)


def run_odds(url: str, outpath: str):
    c = OddsCollector(rate_limit_sec=1.0)
    c.fetch_and_save(url, Path(outpath))


def main():
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--league")
    p.add_argument("--season")
    p.add_argument("--outdir", default="data/raw/sofascore")
    p.add_argument("--odds-url")
    p.add_argument("--odds-out", default="data/odds/odds_snapshot.json")
    args = p.parse_args()

    if args.league and args.season:
        run_sofascore(args.league, args.season, args.outdir)
    if args.odds_url:
        run_odds(args.odds_url, args.odds_out)


if __name__ == "__main__":
    main()
