import os
import pandas as pd
from src.linking.match_linker import link_matches

p = os.path.join('data','processed','matches_canonical.csv')
if not os.path.exists(p):
    print(f'{p} not found')
    raise SystemExit(0)

df = pd.read_csv(p, parse_dates=['match_date_utc'])

# Run A
linked_a = link_matches(df)
# Run B
linked_b = link_matches(df)

cols = ['source_id','canonical_match_id','match_status','match_confidence','matching_reason']
ma = linked_a[cols].set_index('source_id').sort_index()
mb = linked_b[cols].set_index('source_id').sort_index()

cmp = ma.compare(mb)
if cmp.empty:
    print('No differences between runs (deterministic for selected columns).')
else:
    print('Differences found:')
    print(cmp)
    print('\nSample A vs B mismatches:')
    print(ma[ma.index.isin(cmp.index)].head().to_string())
    print(mb[mb.index.isin(cmp.index)].head().to_string())

# summary counts
print('\nRun A total:', len(ma))
print('Run B total:', len(mb))
print('Compared columns identical count:', len(ma) - len(cmp))
