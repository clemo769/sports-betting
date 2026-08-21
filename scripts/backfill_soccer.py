"""Backfill script for soccer historical fixtures using SofaScore scraper.

This script demonstrates how to run the scraper against a league and season
and save a small set of match pages for later processing.
"""
import argparse
import logging
from pathlib import Path
from src.scrapers.sofascore_scraper import SofaScoreScraper


def backfill(league: str, season: str, outdir: str, limit: int = 50):
    s = SofaScoreScraper(rate_limit_sec=1.0)
    matches = s.fetch_match_list(league, season)
    outp = Path(outdir)
    outp.mkdir(parents=True, exist_ok=True)
    count = 0
    for m in matches:
        if count >= limit:
            break
        url = m.get("match_url")
        if not url:
            continue
        data = s.fetch_match_events(url)
        if not data:
            continue
        fname = outp / f"match_{count}.json"
        s.save_raw(data, fname)
        logging.info("Saved %s", fname)
        count += 1


def main():
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True)
    p.add_argument("--season", required=True)
    p.add_argument("--outdir", default="data/raw/sofascore_backfill")
    p.add_argument("--limit", default=20, type=int)
    args = p.parse_args()
    backfill(args.league, args.season, args.outdir, args.limit)


if __name__ == "__main__":
    main()
