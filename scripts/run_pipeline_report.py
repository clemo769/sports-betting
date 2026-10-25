import os
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd

root = Path.cwd()
proc = Path('data/processed')
raw_fdco = Path('data/raw/football-data-co-uk')
raw_sofa = Path('data/raw/sofascore_backfill')

print('Working dir:', root)

# Counts for FDCO parsed outputs
matches_path = proc / 'matches_canonical.csv'
labels_path = proc / 'labels.csv'
features_path = proc / 'features_batch.parquet'

if matches_path.exists():
    m = pd.read_csv(matches_path)
    print('matches_rows', len(m))
else:
    print('matches_canonical.csv: MISSING')

if labels_path.exists():
    l = pd.read_csv(labels_path)
    print('labels_rows', len(l))
else:
    print('labels.csv: MISSING')

# Check SofaScore raw files
sofa_files = list(raw_sofa.glob('*.json')) if raw_sofa.exists() else []
print('sofascore_raw_files_count', len(sofa_files))
sofa_matches = []
for f in sofa_files:
    try:
        j = json.loads(f.read_text(encoding='utf-8'))
        home = j.get('home_team') or j.get('home') or j.get('home_team_name') or j.get('home_team_name')
        away = j.get('away_team') or j.get('away') or j.get('away_team_name') or j.get('away_team_name')
        sofa_matches.append((str(f), home, away))
    except Exception:
        continue

# Attempt join by canonical names if both sources present
unmatched = []
joined = 0
if matches_path.exists() and len(sofa_matches) > 0:
    mnames = set()
    for _, row in m.iterrows():
        mnames.add((row['home_team'], row['away_team']))
    for f, h, a in sofa_matches:
        if (h, a) in mnames:
            joined += 1
        else:
            unmatched.append((f, h, a))
else:
    # No sofa data to join
    joined = 0
    if len(sofa_matches) == 0:
        unmatched = []

print('sofascore_matches_joined', joined)
print('sofascore_unmatched_count', len(unmatched))
if unmatched:
    print('\nUnmatched SofaScore entries (file, home, away):')
    for u in unmatched:
        print(u)

# Show 5 sample feature rows
if features_path.exists():
    try:
        feats = pd.read_parquet(features_path)
        print('\nFeatures sample (5 rows):')
        print(feats.head(5).to_string(index=False))
    except Exception as e:
        print('Failed to read features parquet:', e)
else:
    print('features_batch.parquet: MISSING')

# Run training script and capture metrics
print('\nRunning training...')
train_cmd = [sys.executable, 'src/models/train_baseline.py', '--features', str(features_path), '--labels', str(labels_path), '--out', 'models']
try:
    subprocess.check_call(train_cmd)
    metrics_file = Path('models/metrics.txt')
    if metrics_file.exists():
        print('\nTraining metrics:')
        print(metrics_file.read_text())
    else:
        print('Training finished but metrics file missing')
except subprocess.CalledProcessError as e:
    print('Training failed with exit code', e.returncode)

print('\nReport complete.')
