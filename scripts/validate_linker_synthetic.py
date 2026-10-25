import pandas as pd
from src.linking.match_linker import link_matches


def make_row(source, source_id, league, season, home, away, dt):
    return {'source': source,'source_id': source_id,'league': league,'season': season,'home_team': home,'away_team': away,'match_date_utc': dt}


def build_synthetic():
    rows = []
    # 1) Exact match across sources
    rows.append(make_row('fdco', 'r1', 'premier-league', '23/24', 'Man Utd', 'Arsenal', '2024-05-12T15:00:00Z'))
    rows.append(make_row('sofa', 's1', 'premier-league', '23/24', 'Manchester United', 'Arsenal FC', '2024-05-12T15:00:00Z'))

    # 2) Alias match
    rows.append(make_row('fdco', 'r2', 'premier-league', '23/24', 'Spurs', 'Chelsea', '2024-04-10T14:00:00Z'))
    rows.append(make_row('other', 'o2', 'premier-league', '23/24', 'Tottenham Hotspur', 'Chelsea FC', '2024-04-10T14:00:00Z'))

    # 3) Different times -> should be unmatched
    rows.append(make_row('fdco', 'r3', 'premier-league', '23/24', 'Leeds', 'Everton', '2024-03-01T12:00:00Z'))
    rows.append(make_row('other', 'o3', 'premier-league', '23/24', 'Leeds United', 'Everton FC', '2024-03-01T15:00:00Z'))

    # 4) Missing time on one source, single candidate via date-only -> date_fallback
    rows.append(make_row('fdco', 'r4', 'premier-league', '23/24', 'Brighton', 'Norwich', None))
    rows.append(make_row('other', 'o4', 'premier-league', '23/24', 'Brighton & Hove Albion', 'Norwich City', '2024-02-20T16:00:00Z'))

    # 5) Missing time, multiple candidates -> ambiguous
    rows.append(make_row('fdco', 'r5', 'premier-league', '23/24', 'City A', 'City B', None))
    rows.append(make_row('other', 'o5a', 'premier-league', '23/24', 'City A', 'City B', '2024-06-01T13:00:00Z'))
    rows.append(make_row('other', 'o5b', 'premier-league', '23/24', 'City A', 'City B', '2024-06-01T15:00:00Z'))

    # 6) Duplicate source record
    rows.append(make_row('fdco', 'dup1', 'premier-league', '23/24', 'Burnley', 'Leicester', '2024-01-10T12:00:00Z'))
    rows.append(make_row('fdco', 'dup1', 'premier-league', '23/24', 'Burnley', 'Leicester', '2024-01-10T12:00:00Z'))

    # 7) Different competition names
    rows.append(make_row('fdco', 'r7', 'championship', '23/24', 'Hull', 'Swansea', '2024-05-05T14:00:00Z'))
    rows.append(make_row('other', 'o7', 'premier-league', '23/24', 'Hull City', 'Swansea City', '2024-05-05T14:00:00Z'))

    # 8) Identical keys across sources
    rows.append(make_row('fdco', 'r8a', 'premier-league', '23/24', 'Aston Villa', 'Wolves', '2024-04-01T14:00:00Z'))
    rows.append(make_row('other', 'r8b', 'premier-league', '23/24', 'Aston Villa', 'Wolverhampton', '2024-04-01T14:00:00Z'))

    return pd.DataFrame(rows)


def run_once(df):
    linked = link_matches(df)
    return linked


def compare_runs(df):
    a = run_once(df)
    b = run_once(df)
    # compare canonical ids, status, confidence per source_id
    merged = a.merge(b, on='source_id', suffixes=('_a','_b'))
    diffs = merged[(merged['canonical_match_id_a'] != merged['canonical_match_id_b']) | (merged['match_status_a'] != merged['match_status_b']) | (merged['match_confidence_a'] != merged['match_confidence_b'])]
    return a, b, diffs


if __name__ == '__main__':
    df = build_synthetic()
    a, b, diffs = compare_runs(df)
    print('--- Run A ---')
    print(a[['source_id','match_status','matching_reason','match_confidence','canonical_match_id']].to_string(index=False))
    print('\n--- Run B ---')
    print(b[['source_id','match_status','matching_reason','match_confidence','canonical_match_id']].to_string(index=False))
    print('\n--- Diffs ---')
    if diffs.empty:
        print('No differences between runs (deterministic).')
    else:
        print(diffs[['source_id','canonical_match_id_a','canonical_match_id_b','match_status_a','match_status_b','match_confidence_a','match_confidence_b']].to_string(index=False))
    # write CSV reports if produced
    import os
    rep_dir = os.path.join('data','processed')
    for fname in ['match_link_duplicates.csv','match_link_ambiguous.csv','match_link_unmatched.csv']:
        p = os.path.join(rep_dir,fname)
        if os.path.exists(p):
            print(f"\nSample from {p}:")
            with open(p,'r',encoding='utf-8') as f:
                for i,line in enumerate(f):
                    print(line.strip())
                    if i>=5:
                        break
        else:
            print(f"\n{p} does not exist or is empty.")
