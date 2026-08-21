"""SofaScore scraper (initial version).

This module provides a simple, configurable scraper for extracting match lists
and match event pages from SofaScore. It's a best-effort implementation and
should be adapted to the site's current HTML structure and ToS.
"""
import requests
from bs4 import BeautifulSoup
import time
import json
import logging
from pathlib import Path
from typing import List, Dict, Optional

LOG = logging.getLogger("sofascore_scraper")


class SofaScoreScraper:
    def __init__(self, rate_limit_sec: float = 1.0, user_agent: Optional[str] = None):
        self.session = requests.Session()
        ua = user_agent or "Mozilla/5.0 (compatible; SportsPredBot/0.1)"
        self.session.headers.update({"User-Agent": ua})
        self.rate_limit_sec = rate_limit_sec

    def _get(self, url: str) -> Optional[str]:
        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            time.sleep(self.rate_limit_sec)
            return resp.text
        except Exception as e:
            LOG.exception("GET failed: %s", url)
            return None

    def fetch_match_list(self, league_slug: str, season: str) -> List[Dict]:
        """Fetch a season match list for a league.

        This is a lightweight implementation that attempts to find match links
        from a league season page. Adjust selectors for current SofaScore HTML.
        Returns a list of dicts: {match_url, home, away, match_date}
        """
        url = f"https://www.sofascore.com/tournament/football/{league_slug}/{season}"
        html = self._get(url)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        matches = []
        # NOTE: selectors are approximate and may need tuning
        for a in soup.select("a[href*='/match/']")[:500]:
            href = a.get("href")
            if not href:
                continue
            match_url = "https://www.sofascore.com" + href
            text = a.get_text(strip=True)
            # best-effort parse
            matches.append({"match_url": match_url, "title": text})
        return matches

    def fetch_match_events(self, match_url: str) -> Dict:
        """Fetch structured match event data from a match page.

        Returns a dict with raw events and metadata. Parsing is defensive and
        meant as a starting point.
        """
        html = self._get(match_url)
        if not html:
            return {}
        soup = BeautifulSoup(html, "lxml")
        # Attempt to find embedded JSON data in the page
        data = {}
        for script in soup.find_all("script"):
            if script.string and "window.__INITIAL_STATE__" in script.string:
                try:
                    txt = script.string
                    # Extract JSON after the identifier
                    idx = txt.find("window.__INITIAL_STATE__ = ")
                    if idx != -1:
                        jtext = txt[idx + len("window.__INITIAL_STATE__ = "):]
                        # Trim trailing ;
                        jtext = jtext.strip().rstrip(";")
                        data = json.loads(jtext)
                        break
                except Exception:
                    continue

        # Fallback: extract minimal metadata
        if not data:
            data = {
                "match_url": match_url,
                "title": soup.title.string if soup.title else None,
            }

        return data

    def save_raw(self, data: Dict, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True)
    p.add_argument("--season", required=True)
    p.add_argument("--outdir", default="data/raw/sofascore")
    args = p.parse_args()

    s = SofaScoreScraper(rate_limit_sec=1.0)
    matches = s.fetch_match_list(args.league, args.season)
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    for i, m in enumerate(matches):
        LOG.info("Saving match %d: %s", i, m.get("match_url"))
        filename = out / f"match_{i}.json"
        s.save_raw(m, filename)
