"""Match linker with multi-source-aware logic and reporting.

This linker is conservative: exact datetime + canonicalized teams + competition
must match for an exact match. It detects duplicates, ambiguous date-only fallbacks,
and produces CSV reports for manual inspection.
"""
from pathlib import Path
import pandas as pd
import uuid
from datetime import datetime
from src.normalization.team_aliases import to_canonical


def canonical_team_name(name):
    canonical = to_canonical(name)
    if canonical and canonical.lower().endswith(" fc"):
        canonical = to_canonical(canonical[:-3].strip())
    return canonical


def canonical_key_components(row):
    league = row.get("league") or ""
    season = row.get("season") or ""
    home = (canonical_team_name(row.get("home_team") or "") or "").strip().lower()
    away = (canonical_team_name(row.get("away_team") or "") or "").strip().lower()
    dt = row.get("match_date_utc")
    dt_parsed = None
    dt_iso = ""
    date_only = ""
    if pd.notna(dt) and dt is not None:
        try:
            dt_parsed = pd.to_datetime(dt)
            dt_iso = dt_parsed.isoformat()
            date_only = dt_parsed.date().isoformat()
        except Exception:
            dt_iso = str(dt)
    return league, season, home, away, dt_parsed, dt_iso, date_only


def link_matches(matches_df: pd.DataFrame):
    df = matches_df.copy()
    if "match_date_utc" in df.columns:
        df["match_date_utc"] = pd.to_datetime(df["match_date_utc"], errors="coerce")

    comps = df.apply(canonical_key_components, axis=1)
    comps_df = pd.DataFrame(comps.tolist(), columns=["league_k", "season_k", "home_k", "away_k", "dt_parsed", "dt_iso", "date_only"])
    df = pd.concat([df.reset_index(drop=True), comps_df], axis=1)

    # detect duplicates (same source & source_id)
    if "source" in df.columns and "source_id" in df.columns:
        df["duplicate_count"] = df.groupby(["source", "source_id"])['source_id'].transform('count')
        df["is_duplicate"] = df["duplicate_count"] > 1
    else:
        df["duplicate_count"] = 1
        df["is_duplicate"] = False

    # primary full key (exact match candidate)
    df["primary_key"] = df.apply(lambda r: f"{r['league_k']}|{r['season_k']}|{r['home_k']}|{r['away_k']}|{r['dt_iso']}" if r['dt_iso'] else "", axis=1)
    df["date_key"] = df.apply(lambda r: f"{r['league_k']}|{r['season_k']}|{r['home_k']}|{r['away_k']}|{r['date_only']}" if r['date_only'] else "", axis=1)

    results = []

    # exact matches by primary_key
    prim_groups = df[df['primary_key'] != ""].groupby('primary_key')
    for key, g in prim_groups:
        canon_id = uuid.uuid5(uuid.NAMESPACE_URL, key).hex
        # Multiple independent sources are required before this can be called a
        # cross-source match; same-source rows remain canonicalized duplicates.
        if len(g) > 1:
            independent_sources = g['source'].dropna().astype(str).replace('', pd.NA).dropna().nunique() if 'source' in g.columns else 0
            if independent_sources > 1:
                status = 'matched'
                reason = 'exact_key'
                confidence = 1.0
            elif ('source' in df.columns) and ('source_id' in df.columns):
                status = 'matched'
                reason = 'exact_key_single_source_duplicate'
                confidence = 1.0
            else:
                status = 'ambiguous'
                reason = 'duplicate_key_no_source_info'
                confidence = 0.5
            for _, row in g.iterrows():
                out = row.to_dict()
                out['canonical_match_id'] = canon_id
                out['match_status'] = status
                out['match_confidence'] = confidence
                out['matching_reason'] = reason
                results.append(out)
        else:
            # singleton primary_key: ensure there's no conflicting time candidates
            row = g.iloc[0]
            team_candidates = df[(df['league_k'] == row['league_k']) & (df['season_k'] == row['season_k']) & (df['home_k'] == row['home_k']) & (df['away_k'] == row['away_k'])]
            distinct_times = team_candidates['dt_iso'].nunique()
            if distinct_times > 1:
                # conflicting times across sources -> do not assume match
                out = row.to_dict()
                out['canonical_match_id'] = uuid.uuid5(uuid.NAMESPACE_URL, key).hex
                out['match_status'] = 'unmatched'
                out['match_confidence'] = 0.0
                out['matching_reason'] = 'conflicting_times'
                results.append(out)
            else:
                # detect same teams+time reported under different competitions -> conservative unmatched
                cross_league = df[(df['home_k'] == row['home_k']) & (df['away_k'] == row['away_k']) & (df['dt_iso'] == row['dt_iso']) & (df['league_k'] != row['league_k'])]
                if len(cross_league) > 0:
                    out = row.to_dict()
                    out['canonical_match_id'] = uuid.uuid5(uuid.NAMESPACE_URL, key).hex
                    out['match_status'] = 'unmatched'
                    out['match_confidence'] = 0.0
                    out['matching_reason'] = 'conflicting_competitions'
                    results.append(out)
                    continue
                out = row.to_dict()
                out['canonical_match_id'] = canon_id
                if ('source' in df.columns) and ('source_id' in df.columns):
                    out['match_status'] = 'unmatched'
                    out['match_confidence'] = 0.0
                    out['matching_reason'] = 'no_independent_source_candidate'
                else:
                    out['match_status'] = 'matched'
                    out['match_confidence'] = 1.0
                    out['matching_reason'] = 'exact_key_singleton'
                results.append(out)

    # rows without primary_key -> use date-only fallback or mark unmatched/ambiguous
    no_time = df[df['primary_key'] == ""]
    for idx, row in no_time.iterrows():
        dk = row['date_key']
        if not dk:
            out = row.to_dict()
            # try to find team-matching candidates with dates
            team_candidates = df[(df['league_k'] == row['league_k']) & (df['season_k'] == row['season_k']) & (df['home_k'] == row['home_k']) & (df['away_k'] == row['away_k']) & (df['primary_key'] != "")]
            if 'source' in df.columns:
                team_candidates = team_candidates[team_candidates['source'] != row.get('source')]
            if len(team_candidates) == 1:
                cand = team_candidates.iloc[0]
                canon_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{cand['primary_key']}").hex
                out['canonical_match_id'] = canon_id
                out['match_status'] = 'matched'
                out['match_confidence'] = 0.8
                out['matching_reason'] = 'date_fallback'
                results.append(out)
                continue
            elif len(team_candidates) > 1:
                # if all candidates share the same exact datetime, treat as date_fallback; else ambiguous
                unique_times = team_candidates['dt_iso'].nunique()
                if unique_times == 1:
                    cand = team_candidates.iloc[0]
                    canon_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{cand['primary_key']}").hex
                    out['canonical_match_id'] = canon_id
                    out['match_status'] = 'matched'
                    out['match_confidence'] = 0.8
                    out['matching_reason'] = 'date_fallback'
                    results.append(out)
                    continue
                else:
                    out['canonical_match_id'] = ''
                    out['match_status'] = 'ambiguous'
                    out['match_confidence'] = 0.5
                    out['matching_reason'] = 'multiple_date_candidates'
                    results.append(out)
                    continue
            # no candidates with dates -> deterministic fallback id and unmatched
            fallback_key = f"{row.get('source','')}-{row.get('source_id','')}-{row.get('league_k','')}-{row.get('season_k','')}-{row.get('home_k','')}-{row.get('away_k','')}"
            out['canonical_match_id'] = uuid.uuid5(uuid.NAMESPACE_URL, fallback_key).hex
            out['match_status'] = 'unmatched'
            out['match_confidence'] = 0.0
            out['matching_reason'] = 'no_date_info'
            results.append(out)
            continue

        candidates = df[(df['date_key'] == dk) & (df['primary_key'] != "")]
        if 'source' in df.columns:
            candidates = candidates[candidates['source'] != row.get('source')]
        if len(candidates) == 1:
            cand = candidates.iloc[0]
            canon_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{cand['primary_key']}").hex
            out = row.to_dict()
            out['canonical_match_id'] = canon_id
            out['match_status'] = 'matched'
            out['match_confidence'] = 0.8
            out['matching_reason'] = 'date_fallback'
            results.append(out)
        elif len(candidates) > 1:
            out = row.to_dict()
            out['canonical_match_id'] = ''
            out['match_status'] = 'ambiguous'
            out['match_confidence'] = 0.5
            out['matching_reason'] = 'multiple_date_candidates'
            results.append(out)
        else:
            out = row.to_dict()
            out['canonical_match_id'] = uuid.uuid5(uuid.NAMESPACE_URL, dk).hex
            out['match_status'] = 'unmatched'
            out['match_confidence'] = 0.0
            out['matching_reason'] = 'no_candidates'
            results.append(out)

    result = pd.DataFrame(results)

    # produce CSV reports
    out_dir = Path('data/processed')
    out_dir.mkdir(parents=True, exist_ok=True)
    duplicates = result[result['is_duplicate']]
    ambiguous = result[result['match_status'] == 'ambiguous']
    unmatched = result[result['match_status'] == 'unmatched']
    if not duplicates.empty:
        duplicates.to_csv(out_dir / 'match_link_duplicates.csv', index=False)
    if not ambiguous.empty:
        ambiguous.to_csv(out_dir / 'match_link_ambiguous.csv', index=False)
    if not unmatched.empty:
        unmatched.to_csv(out_dir / 'match_link_unmatched.csv', index=False)

    return result


def cli(in_path: Path, out_path: Path):
    input_paths = [in_path] if isinstance(in_path, Path) else in_path
    frames = [
        pd.read_csv(path, parse_dates=["match_date_utc"]) if path.suffix == ".csv" else pd.read_parquet(path)
        for path in input_paths
    ]
    df = pd.concat(frames, ignore_index=True)
    linked = link_matches(df)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    linked.to_parquet(out_path, index=False)
    sources = sorted({str(source) for source in linked.get('source', pd.Series(dtype=str)).dropna() if str(source)})
    records_by_source = {
        source: int((linked['source'] == source).sum())
        for source in sources
    }
    fdco_records = int((linked['source'] == 'football-data.co.uk').sum()) if 'source' in linked.columns else 0
    report = {
        'fdco_records_processed': fdco_records,
        'canonical_ids_generated': int(linked['canonical_match_id'].nunique()),
        'duplicate_status': f"{int(linked['is_duplicate'].sum())} duplicate records",
        'cross_source_validation_performed': 'YES' if len(sources) > 1 else 'NO',
        'independent_sources_involved': ', '.join(sources) if sources else 'none recorded',
        'records_by_source': ', '.join(f'{source}={count}' for source, count in records_by_source.items()) or 'none recorded',
        'total': len(linked),
        'matched': int((linked['match_status'] == 'matched').sum()),
        'ambiguous': int((linked['match_status'] == 'ambiguous').sum()),
        'unmatched': int((linked['match_status'] == 'unmatched').sum()),
    }
    summary_path = out_path.parent / 'match_link_summary.txt'
    with summary_path.open('w') as f:
        for k, v in report.items():
            f.write(f"{k}: {v}\n")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--input", nargs="+", default=["data/processed/matches_canonical.csv"])
    p.add_argument("--out", default="data/processed/matches_linked.parquet")
    args = p.parse_args()
    cli([Path(path) for path in args.input], Path(args.out))
