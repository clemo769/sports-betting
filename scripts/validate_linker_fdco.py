import argparse
import os
import pandas as pd
from src.linking.match_linker import link_matches

parser = argparse.ArgumentParser()
parser.add_argument('--input', nargs='+', default=[os.path.join('data', 'processed', 'matches_canonical.csv')])
args = parser.parse_args()
paths = args.input
missing = [path for path in paths if not os.path.exists(path)]
if missing:
    print(f"Missing input file(s): {', '.join(missing)}")
    raise SystemExit(0)

frames = [pd.read_csv(path, parse_dates=['match_date_utc']) for path in paths]
df = pd.concat(frames, ignore_index=True)
linked = link_matches(df)

total = len(df)
unique_canon = linked['canonical_match_id'].nunique()
duplicates = int(linked['is_duplicate'].sum()) if 'is_duplicate' in linked.columns else 0
ambiguous = int((linked['match_status'] == 'ambiguous').sum())
unmatched = int((linked['match_status'] == 'unmatched').sum())
matched = int((linked['match_status'] == 'matched').sum())
match_pct = matched / total * 100 if total else 0.0
sources = sorted({str(source) for source in linked['source'].dropna() if str(source)}) if 'source' in linked.columns else []
fdco_records = int((linked['source'] == 'football-data.co.uk').sum()) if 'source' in linked.columns else 0
records_by_source = linked['source'].value_counts().to_dict() if 'source' in linked.columns else {}

print('FDCO records processed:', fdco_records)
print('canonical IDs generated:', unique_canon)
print('duplicate status:', f'{duplicates} duplicate records')
print('cross-source validation performed:', 'YES' if len(sources) > 1 else 'NO')
print('independent sources involved:', ', '.join(sources) if sources else 'none recorded')
print('records by source:', ', '.join(f'{source}={count}' for source, count in records_by_source.items()) or 'none recorded')
print('total records:', total)
print('ambiguous:', ambiguous)
print('unmatched:', unmatched)
print('matched:', matched)
print(f'match percentage: {match_pct:.2f}%')

print('\nTop 10 canonical IDs:')
print(linked['canonical_match_id'].value_counts().head(10).to_string())

# write linked to temp output for inspection
out_p = os.path.join('data','processed','matches_linked_snapshot.parquet')
linked.to_parquet(out_p, index=False)
print('\nWrote snapshot to', out_p)
