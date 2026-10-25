"""OddsPortal parser (best-effort).

OddsPortal HTML structure can change; this parser extracts visible odds tables
for a match page and returns a structured snapshot. Use responsibly and respect
site ToS.
"""
from bs4 import BeautifulSoup
from typing import List, Dict


def parse_oddsportal_match(html: str) -> List[Dict]:
    """Parse OddsPortal match page and extract odds tables.

    Returns a list of dicts: {bookie, market, selection, price}
    """
    soup = BeautifulSoup(html, "lxml")
    results = []
    # OddsPortal often has tables with class 'odds' or 'table-main'
    for table in soup.select("table"):
        # Heuristic: table contains decimal numbers representing odds
        cols = table.select("td")
        text = " ".join([c.get_text(strip=True) for c in cols])
        if not any(ch.isdigit() for ch in text):
            continue
        # Try to read rows
        for tr in table.select("tr"):
            tds = tr.select("td")
            if len(tds) < 2:
                continue
            row = [td.get_text(strip=True) for td in tds]
            # Heuristic mapping
            try:
                if len(row) >= 3:
                    bookie = row[0]
                    sel = row[1]
                    price = row[2]
                else:
                    bookie = None
                    sel = row[0]
                    price = row[1]
                # Normalize price
                price_val = None
                try:
                    price_val = float(price)
                except Exception:
                    # remove non-numeric
                    import re
                    m = re.search(r"\d+\.?\d*", price)
                    if m:
                        price_val = float(m.group(0))
                # Validate price: only keep plausible numeric odds
                if price_val is not None and (price_val > 1.0 and price_val < 1000):
                    results.append({"bookie": bookie, "selection": sel, "price": price_val})
            except Exception:
                continue
    return results
