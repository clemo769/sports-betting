"""Validate the real FDCO/OpenFootball multi-source linking pipeline."""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from src.linking.match_linker import canonical_team_name, link_matches


REQUIRED_COLUMNS = {
    "source",
    "source_id",
    "league",
    "season",
    "home_team",
    "away_team",
    "match_date_utc",
}
STABLE_COLUMNS = [
    "source",
    "source_id",
    "league_k",
    "season_k",
    "home_k",
    "away_k",
    "date_only",
    "source_event_ts",
    "dt_iso",
    "canonical_match_id",
    "matched_source_ids",
    "match_status",
    "match_confidence",
    "matching_reason",
]


def load_source(path: Path, expected_source: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")
    frame["_input_file"] = str(path)
    frame["_expected_source"] = expected_source
    return frame


def input_quality(frame: pd.DataFrame) -> dict[str, int]:
    source_id = frame["source_id"].fillna("").astype(str).str.strip()
    source = frame["source"].fillna("").astype(str).str.strip()
    teams_missing = frame[["home_team", "away_team"]].isna().any(axis=1)
    teams_missing |= frame[["home_team", "away_team"]].fillna("").astype(str).apply(
        lambda column: column.str.strip().eq("")
    ).any(axis=1)
    dates = pd.to_datetime(frame["match_date_utc"], errors="coerce")
    malformed = dates.isna() & frame["match_date_utc"].notna()
    return {
        "missing_source_identifiers": int(source_id.eq("").sum()),
        "duplicate_source_ids": int(frame.assign(_source_id=source_id).duplicated(["source", "_source_id"], keep=False).sum()),
        "missing_teams": int(teams_missing.sum()),
        "missing_event_dates": int(frame["match_date_utc"].isna().sum()),
        "malformed_event_dates": int(malformed.sum()),
        "unexpected_source_names": int((~source.isin({"football-data.co.uk", "openfootball"})).sum()),
    }


def stable_results(linked: pd.DataFrame) -> pd.DataFrame:
    source_ids = linked.assign(
        _source_ref=linked["source"].fillna("").astype(str) + ":" + linked["source_id"].fillna("").astype(str)
    ).groupby("canonical_match_id")["_source_ref"].transform(lambda values: "|".join(sorted(values)))
    comparable = linked.copy()
    comparable["matched_source_ids"] = source_ids.to_numpy()
    stable = comparable.reindex(columns=STABLE_COLUMNS).copy()
    stable["source_id"] = stable["source_id"].fillna("").astype(str)
    stable["_occurrence"] = stable.groupby(["source", "source_id"], dropna=False).cumcount()
    return stable.sort_values(["source", "source_id", "_occurrence"], kind="mergesort").reset_index(drop=True)


def compare_stable(first: pd.DataFrame, second: pd.DataFrame) -> pd.DataFrame:
    left = stable_results(first).add_suffix("_run_a")
    right = stable_results(second).add_suffix("_run_b")
    key = ["source_run_a", "source_id_run_a", "_occurrence_run_a"]
    merged = left.merge(
        right,
        left_on=key,
        right_on=[name.replace("_run_a", "_run_b") for name in key],
        how="outer",
        indicator=True,
    )
    differences = merged[merged["_merge"] != "both"].copy()
    common = merged[merged["_merge"] == "both"]
    for column in STABLE_COLUMNS:
        left_column = f"{column}_run_a"
        right_column = f"{column}_run_b"
        unequal = common[left_column].fillna("<NA>").astype(str) != common[right_column].fillna("<NA>").astype(str)
        differences = pd.concat([differences, common[unequal]], ignore_index=True)
    return differences.drop_duplicates().reset_index(drop=True)


def _legacy_summary(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                values[key.strip()] = value.strip()
    return values


def run_validation(fdco_path: Path, openfootball_path: Path, out_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    out_dir.mkdir(parents=True, exist_ok=True)
    legacy = _legacy_summary(out_dir / "match_link_summary.txt")
    fdco = load_source(fdco_path, "football-data.co.uk")
    openfootball = load_source(openfootball_path, "openfootball")
    inputs = pd.concat([fdco, openfootball], ignore_index=True)
    quality = input_quality(inputs)
    linked_a = link_matches(inputs)
    linked_b = link_matches(inputs)
    differences = compare_stable(linked_a, linked_b)

    linked_a["normalized_home_team"] = linked_a["home_team"].map(canonical_team_name)
    linked_a["normalized_away_team"] = linked_a["away_team"].map(canonical_team_name)
    linked_a.to_parquet(out_dir / "multi_source_validation_linked.parquet", index=False)
    differences.to_csv(out_dir / "multi_source_validation_determinism.csv", index=False)
    linked_a[linked_a["match_status"] == "unmatched"].to_csv(out_dir / "multi_source_validation_unmatched.csv", index=False)
    linked_a[linked_a["match_status"] == "ambiguous"].to_csv(out_dir / "multi_source_validation_ambiguous.csv", index=False)
    linked_a[linked_a["is_duplicate"]].to_csv(out_dir / "multi_source_validation_duplicates.csv", index=False)

    total = len(linked_a)
    reasons = linked_a["matching_reason"].value_counts().sort_index()
    pairs = (
        linked_a[linked_a["match_status"] == "matched"]
        .groupby("canonical_match_id")["source"]
        .agg(lambda values: " + ".join(sorted(set(values))))
        .value_counts()
        .sort_index()
    )
    canonical_counts = linked_a["canonical_match_id"].value_counts()
    source_counts = linked_a.groupby("canonical_match_id")["source"].nunique()
    legitimate_multi_source_relationships = int((source_counts > 1).sum())
    same_source_canonical_collisions = int(
        linked_a.groupby(["canonical_match_id", "source"]).size().gt(1).groupby(level=0).any().sum()
    )
    dates = pd.to_datetime(linked_a["match_date_utc"], errors="coerce")
    conflicting_times = int((linked_a["matching_reason"] == "conflicting_times").sum())
    conflicting_competitions = int((linked_a["matching_reason"] == "conflicting_competitions").sum())
    summary_lines = [
        "REAL MULTI-SOURCE LINKER VALIDATION",
        "",
        f"fdco_input: {fdco_path}",
        f"openfootball_input: {openfootball_path}",
        f"source_records_fdco: {int((linked_a['source'] == 'football-data.co.uk').sum())}",
        f"source_records_openfootball: {int((linked_a['source'] == 'openfootball').sum())}",
        f"source_records_total: {total}",
        f"canonical_match_count: {linked_a['canonical_match_id'].nunique()}",
        f"canonical_ids: {linked_a['canonical_match_id'].nunique()}",
        f"legitimate_multi_source_relationships: {legitimate_multi_source_relationships}",
        f"matched_source_records: {int((linked_a['match_status'] == 'matched').sum())}",
        f"unmatched_records: {int((linked_a['match_status'] == 'unmatched').sum())}",
        f"ambiguous_records: {int((linked_a['match_status'] == 'ambiguous').sum())}",
        f"actual_duplicate_source_records: {int(linked_a['is_duplicate'].sum())}",
        f"same_source_canonical_collision_count: {same_source_canonical_collisions}",
        f"conflicting_time_records: {conflicting_times}",
        f"conflicting_competition_records: {conflicting_competitions}",
        f"match_rate: {(linked_a['match_status'] == 'matched').mean() if total else 0:.6f}",
        f"unmatched_rate: {(linked_a['match_status'] == 'unmatched').mean() if total else 0:.6f}",
        f"date_range: {dates.min().date() if dates.notna().any() else 'none'} to {dates.max().date() if dates.notna().any() else 'none'}",
        "",
        "INPUT QUALITY (records are not silently discarded):",
    ]
    summary_lines.extend(f"{key}: {value}" for key, value in quality.items())
    summary_lines.extend(["", "COUNTS BY LINKER DECISION/REASON:"])
    summary_lines.extend(f"{key}: {value}" for key, value in reasons.items())
    summary_lines.extend(["", "COUNTS BY MATCHED SOURCE PAIR:"])
    summary_lines.extend(f"{key}: {value}" for key, value in pairs.items())
    summary_lines.extend([
        "",
        "LEGACY ARTIFACT COMPARISON:",
        "The old FDCO validator defaulted to matches_canonical.csv only (380 FDCO rows), so it was single-source and marked every row unmatched.",
        "The prior 612 matched / 148 unmatched summary recorded 760 rows from FDCO and OpenFootball, indicating a two-input run; its exact command is not preserved.",
        f"legacy_summary_total: {legacy.get('total', 'UNVERIFIED')}",
        f"legacy_summary_matched: {legacy.get('matched', 'UNVERIFIED')}",
        f"legacy_summary_unmatched: {legacy.get('unmatched', 'UNVERIFIED')}",
        "",
        "DETERMINISM: PASS" if differences.empty else "DETERMINISM: FAIL",
        f"stable_rows_compared: {len(stable_results(linked_a))}",
        f"stable_differences: {len(differences)}",
    ])
    (out_dir / "multi_source_validation_summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    pd.DataFrame([{
        "source_records_fdco": int((linked_a["source"] == "football-data.co.uk").sum()),
        "source_records_openfootball": int((linked_a["source"] == "openfootball").sum()),
        "source_records_total": total,
        "canonical_match_count": linked_a["canonical_match_id"].nunique(),
        "legitimate_multi_source_relationships": legitimate_multi_source_relationships,
        "matched_source_records": int((linked_a["match_status"] == "matched").sum()),
        "unmatched_records": int((linked_a["match_status"] == "unmatched").sum()),
        "ambiguous_records": int((linked_a["match_status"] == "ambiguous").sum()),
        "actual_duplicate_source_records": int(linked_a["is_duplicate"].sum()),
        "same_source_canonical_collision_count": same_source_canonical_collisions,
        "conflicting_time_records": conflicting_times,
        "conflicting_competition_records": conflicting_competitions,
        "determinism": "PASS" if differences.empty else "FAIL",
    }]).to_csv(out_dir / "multi_source_validation_summary.csv", index=False)
    if not differences.empty:
        raise RuntimeError(f"DETERMINISTIC: FAIL ({len(differences)} stable differences)")
    return linked_a, differences


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fdco", default="data/processed/matches_canonical.csv")
    parser.add_argument("--openfootball", default="data/processed/openfootball_premier-league_202324.csv")
    parser.add_argument("--outdir", default="data/processed")
    args = parser.parse_args()
    out_dir = Path(args.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_validation(Path(args.fdco), Path(args.openfootball), out_dir)
    print(f"Wrote multi-source validation outputs to {out_dir}")
    print("DETERMINISTIC: PASS")


if __name__ == "__main__":
    main()