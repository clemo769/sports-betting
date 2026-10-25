import pandas as pd
from src.linking.match_linker import link_matches

def make_row(source, source_id, league, season, home, away, dt):
    return {'source': source,'source_id': source_id,'league': league,'season': season,'home_team': home,'away_team': away,'match_date_utc': dt}


def test_print():
    rows=[]
    rows.append(make_row('fdco', 'r3', 'premier-league', '23/24', 'Leeds', 'Everton', '2024-03-01T12:00:00Z'))
    rows.append(make_row('other', 'o3', 'premier-league', '23/24', 'Leeds United', 'Everton FC', '2024-03-01T15:00:00Z'))
    df=pd.DataFrame(rows)
    linked=link_matches(df)
    print(linked[['source_id','home_team','away_team','match_date_utc','league_k','home_k','away_k','dt_iso','primary_key','match_status','matching_reason']])
    assert True
