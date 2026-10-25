import pandas as pd
from src.linking.match_linker import link_matches


def test_exact_match():
    df = pd.DataFrame([
        {"league": "premier-league", "season": "2324", "home_team": "A", "away_team": "B", "match_date_utc": "2023-08-01T15:00:00"},
    ])
    linked = link_matches(df)
    assert linked.iloc[0]["match_status"] == "matched"


def test_different_date_not_matched():
    df = pd.DataFrame([
        {"league": "premier-league", "season": "2324", "home_team": "A", "away_team": "B", "match_date_utc": "2023-08-01T15:00:00"},
        {"league": "premier-league", "season": "2324", "home_team": "A", "away_team": "B", "match_date_utc": "2023-09-01T15:00:00"},
    ])
    linked = link_matches(df)
    # two distinct keys -> both matched separately (different canonical ids)
    first_id = linked.iloc[0]["canonical_match_id"]
    assert len(linked[linked["canonical_match_id"] == first_id]) == 1


def test_ambiguous_duplicates():
    df = pd.DataFrame([
        {"league": "premier-league", "season": "2324", "home_team": "A", "away_team": "B", "match_date_utc": "2023-08-01T15:00:00"},
        {"league": "premier-league", "season": "2324", "home_team": "A", "away_team": "B", "match_date_utc": "2023-08-01T15:00:00"},
    ])
    linked = link_matches(df)
    assert (linked["match_status"] == "ambiguous").any()
