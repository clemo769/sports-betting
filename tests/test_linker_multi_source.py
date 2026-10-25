import pandas as pd
from src.linking.match_linker import link_matches


def make_row(source, source_id, league, season, home, away, dt):
    return {
        'source': source,
        'source_id': source_id,
        'league': league,
        'season': season,
        'home_team': home,
        'away_team': away,
        'match_date_utc': dt,
    }


def test_multi_source_scenarios(tmp_path):
    # Build synthetic dataset covering multiple scenarios
    rows = []

    # 1) Exact match across sources
    rows.append(make_row('fdco', 'r1', 'premier-league', '23/24', 'Man Utd', 'Arsenal', '2024-05-12T15:00:00Z'))
    rows.append(make_row('sofa', 's1', 'premier-league', '23/24', 'Manchester United', 'Arsenal FC', '2024-05-12T15:00:00Z'))

    # 2) Alias match with slight name variation (should match exact key after canonicalization)
    rows.append(make_row('fdco', 'r2', 'premier-league', '23/24', 'Spurs', 'Chelsea', '2024-04-10T14:00:00Z'))
    rows.append(make_row('other', 'o2', 'premier-league', '23/24', 'Tottenham Hotspur', 'Chelsea FC', '2024-04-10T14:00:00Z'))

    # 3) Different times -> should be unmatched (no fallback)
    rows.append(make_row('fdco', 'r3', 'premier-league', '23/24', 'Leeds', 'Everton', '2024-03-01T12:00:00Z'))
    rows.append(make_row('other', 'o3', 'premier-league', '23/24', 'Leeds United', 'Everton FC', '2024-03-01T15:00:00Z'))

    # 4) Missing time on one source, single candidate via date-only -> date_fallback match
    rows.append(make_row('fdco', 'r4', 'premier-league', '23/24', 'Brighton', 'Norwich', None))
    rows.append(make_row('other', 'o4', 'premier-league', '23/24', 'Brighton & Hove Albion', 'Norwich City', '2024-02-20T16:00:00Z'))

    # 5) Missing time, multiple candidates on date-only -> ambiguous
    rows.append(make_row('fdco', 'r5', 'premier-league', '23/24', 'City A', 'City B', None))
    rows.append(make_row('other', 'o5a', 'premier-league', '23/24', 'City A', 'City B', '2024-06-01T13:00:00Z'))
    rows.append(make_row('other', 'o5b', 'premier-league', '23/24', 'City A', 'City B', '2024-06-01T15:00:00Z'))

    # 6) Duplicate source record (same source & source_id repeated)
    rows.append(make_row('fdco', 'dup1', 'premier-league', '23/24', 'Burnley', 'Leicester', '2024-01-10T12:00:00Z'))
    rows.append(make_row('fdco', 'dup1', 'premier-league', '23/24', 'Burnley', 'Leicester', '2024-01-10T12:00:00Z'))

    # 7) Different competition names (should not match)
    rows.append(make_row('fdco', 'r7', 'championship', '23/24', 'Hull', 'Swansea', '2024-05-05T14:00:00Z'))
    rows.append(make_row('other', 'o7', 'premier-league', '23/24', 'Hull City', 'Swansea City', '2024-05-05T14:00:00Z'))

    # 8) Duplicate in different sources but identical key -> ambiguous (multiple sources)
    rows.append(make_row('fdco', 'r8a', 'premier-league', '23/24', 'Aston Villa', 'Wolves', '2024-04-01T14:00:00Z'))
    rows.append(make_row('other', 'r8b', 'premier-league', '23/24', 'Aston Villa', 'Wolverhampton', '2024-04-01T14:00:00Z'))

    df = pd.DataFrame(rows)

    linked = link_matches(df)

    # Assertions per scenario
    # scenario 1 and 2: matched
    assert (linked[linked['source_id'].isin(['r1','s1'])]['match_status'] == 'matched').all()
    assert (linked[linked['source_id'].isin(['r2','o2'])]['match_status'] == 'matched').all()

    # scenario 3: unmatched (different times)
    assert (linked[linked['source_id'].isin(['r3','o3'])]['match_status'] == 'unmatched').all()

    # scenario 4: date_fallback match
    assert (linked[linked['source_id'].isin(['r4'])]['matching_reason'] == 'date_fallback').all()

    # scenario 5: ambiguous
    assert (linked[linked['source_id'].isin(['r5'])]['match_status'] == 'ambiguous').all()

    # scenario 6: duplicates
    assert (linked[linked['source_id'].isin(['dup1'])]['is_duplicate']).all()

    # scenario 7: different competitions -> unmatched
    assert (linked[linked['source_id'].isin(['r7','o7'])]['match_status'] == 'unmatched').all()

    # scenario 8: matched (exact key across sources after canonicalization)
    assert (linked[linked['source_id'].isin(['r8a','r8b'])]['match_status'] == 'matched').all()
