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
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
from src.normalization.team_aliases import to_canonical

LOG = logging.getLogger("sofascore_scraper")


class SofaScoreScraper:
    def __init__(self, rate_limit_sec: float = 1.0, user_agent: Optional[str] = None):
        self.session = requests.Session()
        ua = user_agent or "Mozilla/5.0 (compatible; SportsPredBot/0.1)"
        self.session.headers.update({"User-Agent": ua})
        self.rate_limit_sec = rate_limit_sec

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True,
        before_sleep=before_sleep_log(LOG, logging.WARNING),
    )
    def _get(self, url: str) -> Optional[str]:
        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            time.sleep(self.rate_limit_sec)
            return resp.text
        except requests.exceptions.RequestException:
            LOG.warning("Transient request error for %s; will retry if attempts remain", url)
            # Re-raise to trigger tenacity retry
            raise
        except Exception:
            LOG.exception("GET failed: %s", url)
            return None

    def fetch_match_list(self, league_slug: str, season: str) -> List[Dict]:
        """Fetch a season match list for a league.

        This is a lightweight implementation that attempts to find match links
        from a league season page. Adjust selectors for current SofaScore HTML.
        Returns a list of dicts: {match_url, home, away, match_date}
        """
        # SofaScore exposes JSON data embedded in the page; try a league API endpoint first
        url = f"https://www.sofascore.com/tournament/football/{league_slug}/{season}"
        html = self._get(url)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        matches = []
        # Try to find links to matches using anchor tags, fallback to embedded JSON
        for a in soup.select("a[href*='/match/']"):
            href = a.get("href")
            if not href:
                continue
            if "/match/" not in href:
                continue
            match_url = "https://www.sofascore.com" + href
            title = a.get_text(strip=True)
            # Attempt to split title into home/away and canonicalize names
            home_team = None
            away_team = None
            if title:
                for sep in [" - ", " – ", " — ", " vs ", " vs. ", " v ", " v. "]:
                    if sep in title:
                        parts = [p.strip() for p in title.split(sep, 1)]
                        if len(parts) == 2:
                            home_team = to_canonical(parts[0])
                            away_team = to_canonical(parts[1])
                        break
            entry = {"match_url": match_url, "title": title}
            if home_team:
                entry["home_team"] = home_team
            if away_team:
                entry["away_team"] = away_team
            matches.append(entry)

        # If not many matches found, attempt to parse embedded JSON state for fixtures
        if len(matches) < 30:
            for script in soup.find_all("script"):
                if script.string and "window.__INITIAL_STATE__" in script.string:
                    try:
                        txt = script.string
                        idx = txt.find("window.__INITIAL_STATE__ = ")
                        if idx != -1:
                            jtext = txt[idx + len("window.__INITIAL_STATE__ = "):]
                            jtext = jtext.strip().rstrip(";")
                            data = json.loads(jtext)
                            # navigate common paths where fixtures might exist
                            # Best-effort: look for "fixtures" or "events" keys
                            def walk_dict(d):
                                if isinstance(d, dict):
                                    for k, v in d.items():
                                        if k and ("fixture" in k.lower() or "match" in k.lower()):
                                            return v
                                    for v in d.values():
                                        r = walk_dict(v)
                                        if r:
                                            return r
                                elif isinstance(d, list):
                                    for item in d:
                                        r = walk_dict(item)
                                        if r:
                                            return r
                                return None

                            fixtures = walk_dict(data)
                            if fixtures and isinstance(fixtures, list):
                                for item in fixtures[:500]:
                                    try:
                                        if isinstance(item, dict) and item.get("id"):
                                            murl = f"https://www.sofascore.com/match/{item.get('id')}"
                                            title = item.get("name")
                                            # Try to extract explicit home/away from known keys
                                            home = item.get("home") or item.get("homeTeam") or item.get("homeName") or item.get("home_name")
                                            away = item.get("away") or item.get("awayTeam") or item.get("awayName") or item.get("away_name")
                                            if home or away:
                                                home_c = to_canonical(home) if home else None
                                                away_c = to_canonical(away) if away else None
                                                entry = {"match_url": murl, "title": title}
                                                if home_c:
                                                    entry["home_team"] = home_c
                                                if away_c:
                                                    entry["away_team"] = away_c
                                                matches.append(entry)
                                            else:
                                                # Fallback: try splitting the title
                                                home_team = None
                                                away_team = None
                                                if title:
                                                    for sep in [" - ", " – ", " — ", " vs ", " vs. ", " v ", " v. "]:
                                                        if sep in title:
                                                            parts = [p.strip() for p in title.split(sep, 1)]
                                                            if len(parts) == 2:
                                                                home_team = to_canonical(parts[0])
                                                                away_team = to_canonical(parts[1])
                                                            break
                                                entry = {"match_url": murl, "title": title}
                                                if home_team:
                                                    entry["home_team"] = home_team
                                                if away_team:
                                                    entry["away_team"] = away_team
                                                matches.append(entry)
                                    except Exception:
                                        continue
                            break
                    except Exception:
                        continue

        # Deduplicate by URL
        seen = set()
        dedup = []
        for m in matches:
            u = m.get("match_url")
            if not u or u in seen:
                continue
            seen.add(u)
            dedup.append(m)
        return dedup

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
