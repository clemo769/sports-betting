# Extend this mapping as new mismatches are discovered across data sources.
"""Canonical team name mapping across data sources.

Keys are canonical names (used internally and in matches_canonical.csv).
Each value is a list of known aliases used by different sources
(SofaScore, football-data.co.uk, OddsPortal, etc.).
"""
import hashlib
import json
from typing import Dict, List, Optional


TEAM_ALIAS_MAP_VERSION = "team-aliases-v1"

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

TEAM_ALIAS_MAP_SHA256 = hashlib.sha256(
    json.dumps(TEAM_ALIASES, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
).hexdigest()


def team_identity_evidence(
    name: Optional[str],
    *,
    strip_fc_suffix: bool = False,
) -> dict[str, Optional[str]]:
    """Record the exact source label and alias rule used for its display key."""
    if not name:
        return {
            "source_name": name,
            "resolved_name": None,
            "resolution_status": "missing_source_name",
            "resolution_rule": None,
            "mapping_version": TEAM_ALIAS_MAP_VERSION,
            "mapping_sha256": TEAM_ALIAS_MAP_SHA256,
        }

    source_name = str(name)
    normalized_name = source_name.strip()
    mapped_name = _ALIAS_TO_CANONICAL.get(normalized_name.lower())
    resolved_name = mapped_name if mapped_name is not None else normalized_name
    resolution_rule = "alias_map" if mapped_name is not None else "unchanged_unmapped_name"
    mapped_after_suffix = False
    if strip_fc_suffix and resolved_name.lower().endswith(" fc"):
        suffix_free = resolved_name[:-3].strip()
        suffix_mapping = _ALIAS_TO_CANONICAL.get(suffix_free.lower())
        mapped_after_suffix = suffix_mapping is not None
        resolved_name = suffix_mapping or suffix_free
        resolution_rule = f"{resolution_rule}+strip_fc_suffix"
    return {
        "source_name": source_name,
        "resolved_name": resolved_name,
        "resolution_status": (
            "mapped_with_versioned_aliases"
            if mapped_name is not None or mapped_after_suffix
            else "source_name_only"
        ),
        "resolution_rule": resolution_rule,
        "mapping_version": TEAM_ALIAS_MAP_VERSION,
        "mapping_sha256": TEAM_ALIAS_MAP_SHA256,
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
