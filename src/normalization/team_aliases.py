# Extend this mapping as new mismatches are discovered across data sources.
"""Canonical team name mapping across data sources.

Keys are canonical names (used internally and in matches_canonical.csv).
Each value is a list of known aliases used by different sources
(SofaScore, football-data.co.uk, OddsPortal, etc.).
"""
from typing import Dict, List, Optional

TEAM_ALIASES: Dict[str, List[str]] = {
    "Manchester United": ["Man United", "Man Utd", "Manchester Utd", "MUFC"],
    "Manchester City": ["Man City", "Man C", "MCFC"],
    "Tottenham Hotspur": ["Tottenham", "Spurs", "Tottenham H"],
    "Newcastle United": ["Newcastle", "Newcastle Utd", "NUFC"],
    "Wolverhampton Wanderers": ["Wolves", "Wolverhampton"],
    "Leeds United": ["Leeds"],
    "Everton": ["Everton FC"],
    "Brighton & Hove Albion": ["Brighton", "Brighton and Hove Albion", "Brighton H Albion"],
    "Norwich City": ["Norwich"],
    "Bournemouth": ["AFC Bournemouth"],
    "Luton": ["Luton Town"],
    "Hull City": ["Hull"],
    "Swansea City": ["Swansea"],
    "West Ham United": ["West Ham", "West Ham Utd"],
    "Nottingham Forest": ["Nott'm Forest", "Nottm Forest", "Forest"],
    "Leicester City": ["Leicester"],
    "Sheffield United": ["Sheffield Utd", "Sheff Utd"],
    "Real Madrid": ["Real Madrid CF"],
    "Barcelona": ["FC Barcelona", "Barca"],
    "Atletico Madrid": ["Atlético Madrid", "Ath Madrid"],
    "Athletic Bilbao": ["Athletic Club", "Ath Bilbao"],
    "Bayern Munich": ["FC Bayern", "Bayern München", "Bayern Munchen"],
    "Borussia Dortmund": ["Dortmund", "BVB"],
    "Bayer Leverkusen": ["Leverkusen"],
    "RB Leipzig": ["Leipzig"],
    "Paris Saint-Germain": ["PSG", "Paris SG"],
    "Inter Milan": ["Inter", "Internazionale"],
    "AC Milan": ["Milan"],
    "AS Roma": ["Roma"],
    "Juventus": ["Juve"],
}

_ALIAS_TO_CANONICAL: Dict[str, str] = {
    alias.lower(): canonical
    for canonical, aliases in TEAM_ALIASES.items()
    for alias in aliases + [canonical]
}

def to_canonical(name: Optional[str]) -> Optional[str]:
    """Return the canonical team name for any known alias.

    If the name isn't in the alias table, return it unchanged (rather than
    None) so unmapped teams don't silently disappear from the pipeline —
    just won't join correctly across sources until added here.
    """
    if not name:
        return name
    return _ALIAS_TO_CANONICAL.get(name.strip().lower(), name.strip())
