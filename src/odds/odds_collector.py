"""Odds timeseries collector (initial).

This module provides a basic scraper interface to collect odds snapshots for
matches. It is intentionally generic — supply a URL template or implement a
site-specific parser function.
"""
import requests
from bs4 import BeautifulSoup
import json
import time
import logging
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log

LOG = logging.getLogger("odds_collector")


class OddsCollector:
    def __init__(self, rate_limit_sec: float = 1.0, user_agent: str = None):
        self.session = requests.Session()
        ua = user_agent or "Mozilla/5.0 (compatible; SportsOddsBot/0.1)"
        self.session.headers.update({"User-Agent": ua})
        self.rate_limit_sec = rate_limit_sec

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True,
        before_sleep=before_sleep_log(LOG, logging.WARNING),
    )
    def _get(self, url: str):
        try:
            r = self.session.get(url, timeout=15)
            r.raise_for_status()
            time.sleep(self.rate_limit_sec)
            return r.text
        except requests.exceptions.RequestException:
            LOG.warning("Transient odds request error for %s; will retry if attempts remain", url)
            raise
        except Exception:
            LOG.exception("Odds GET failed: %s", url)
            return None

    def parse_odds_from_html(self, html: str) -> list:
        """Parse odds snapshot from provider HTML. Return list of dicts.

        Example output: [{"bookie": "BookieName", "market": "1X2", "selection": "1", "price": 1.95}]
        """
        soup = BeautifulSoup(html, "lxml")
        odds = []
        # Placeholder: implement provider-specific parsing
        # Example: find tables with class 'odds' and extract rows
        for table in soup.select("table"):
            # best-effort
            for tr in table.select("tr"):
                cols = [td.get_text(strip=True) for td in tr.select("td")]
                if len(cols) >= 3:
                    odds.append({"raw_cols": cols})
        return odds

    def fetch_and_save(self, url: str, outpath: Path):
        html = self._get(url)
        if not html:
            return False
        odds = self.parse_odds_from_html(html)
        outpath.parent.mkdir(parents=True, exist_ok=True)
        with outpath.open("w", encoding="utf-8") as f:
            json.dump({"url": url, "odds": odds}, f, ensure_ascii=False, indent=2)
        return True


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--out", default="data/odds/odds_snapshot.json")
    args = p.parse_args()
    c = OddsCollector(rate_limit_sec=1.0)
    c.fetch_and_save(args.url, Path(args.out))
