"""Build time-safe pre-match features from canonical match records."""
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from src.features.temporal_contract import validate_prediction_cutoff


def _event_groups(matches_df: pd.DataFrame):
    """Yield valid event-time groups in deterministic order."""
    times = pd.to_datetime(matches_df["match_date_utc"], errors="coerce")
    valid = matches_df.index[times.notna()]
    keys = matches_df.loc[valid].copy()
    keys["_event_time"] = times.loc[valid]
    keys["_tie_key"] = keys.apply(
        lambda row: "|".join(str(row.get(column, "")) for column in ("match_id", "source", "source_id")),
        axis=1,
    )
    keys = keys.sort_values(["_event_time", "_tie_key"], kind="mergesort")
    for _, group in keys.groupby("_event_time", sort=False):
        yield list(group.index)


def _team_match_points(row: pd.Series):
    home_goals = row.get("home_goals")
    away_goals = row.get("away_goals")
    if pd.isna(home_goals) or pd.isna(away_goals):
        return None, None
    if home_goals > away_goals:
        return 3, 0
    if home_goals < away_goals:
        return 0, 3
    return 1, 1


def compute_elo_series(matches_df: pd.DataFrame, k: float = 20.0):
    """Return pre-match ``(home, away)`` Elo ratings aligned to input rows.

    Results from all matches at an event timestamp are applied after features
    for that timestamp are recorded, preventing same-time cross-match leakage.
    """
    elo = {}
    ratings = {}
    for indices in _event_groups(matches_df):
        for index in indices:
            row = matches_df.loc[index]
            home = row["home_team"]
            away = row["away_team"]
            elo.setdefault(home, 1500.0)
            elo.setdefault(away, 1500.0)
            ratings[index] = (elo[home], elo[away])

        for index in indices:
            row = matches_df.loc[index]
            home = row["home_team"]
            away = row["away_team"]
            home_goals, away_goals = row.get("home_goals"), row.get("away_goals")
            if pd.isna(home_goals) or pd.isna(away_goals):
                continue
            expected_home = 1.0 / (1.0 + 10 ** ((elo[away] - elo[home]) / 400.0))
            score_home = 1.0 if home_goals > away_goals else 0.5 if home_goals == away_goals else 0.0
            elo[home] += k * (score_home - expected_home)
            elo[away] += k * ((1.0 - score_home) - (1.0 - expected_home))

    return [ratings.get(index, (1500.0, 1500.0)) for index in matches_df.index]


def compute_form_series(matches_df: pd.DataFrame, lookback: int = 3):
    """Return pre-match points form keyed by ``(row index, team)``."""
    histories = {}
    form = {}
    for indices in _event_groups(matches_df):
        for index in indices:
            row = matches_df.loc[index]
            for team in (row["home_team"], row["away_team"]):
                previous = histories.get(team, [])
                form[(index, team)] = np.mean(previous[-lookback:]) if len(previous) >= lookback else np.nan

        for index in indices:
            row = matches_df.loc[index]
            home_points, away_points = _team_match_points(row)
            if home_points is not None:
                histories.setdefault(row["home_team"], []).append(home_points)
                histories.setdefault(row["away_team"], []).append(away_points)
    return form


def _sort_for_output(matches_df: pd.DataFrame) -> pd.DataFrame:
    output = matches_df.copy()
    output["_event_time"] = pd.to_datetime(output["match_date_utc"], errors="coerce")
    output["_tie_key"] = output.apply(
        lambda row: "|".join(str(row.get(column, "")) for column in ("match_id", "source", "source_id")),
        axis=1,
    )
    return output.sort_values(["_event_time", "_tie_key"], na_position="last", kind="mergesort").drop(
        columns=["_event_time", "_tie_key"]
    ).reset_index(drop=True)


def _resolve_prediction_cutoffs(df: pd.DataFrame, prediction_cutoff, prediction_cutoffs):
    if prediction_cutoff is not None and prediction_cutoffs is not None:
        raise ValueError("provide either prediction_cutoff or prediction_cutoffs, not both")
    if prediction_cutoffs is None:
        if prediction_cutoff is None:
            return None
        cutoff = validate_prediction_cutoff(prediction_cutoff)
        values = pd.Series(cutoff, index=df.index, dtype="object")
    else:
        if "match_id" not in df.columns:
            raise ValueError("per-match prediction cutoffs require a match_id column")
        if isinstance(prediction_cutoffs, pd.Series):
            values_by_id = prediction_cutoffs.to_dict()
        elif isinstance(prediction_cutoffs, dict):
            values_by_id = prediction_cutoffs
        else:
            raise TypeError("prediction_cutoffs must be a mapping or pandas Series keyed by match_id")
        match_ids = df["match_id"].tolist()
        missing = [match_id for match_id in match_ids if match_id not in values_by_id]
        if missing:
            raise ValueError(f"prediction_cutoffs missing match_id values: {missing[:3]}")
        values = pd.Series(
            [validate_prediction_cutoff(values_by_id[match_id]) for match_id in match_ids],
            index=df.index,
            dtype="object",
        )

    event_times = pd.to_datetime(df["match_date_utc"], errors="coerce")
    for index, event_time in event_times.items():
        if pd.isna(event_time):
            continue
        cutoff = values.loc[index]
        if (event_time.tzinfo is None) != (cutoff.tzinfo is None):
            raise ValueError("match event times and prediction cutoffs must both be timezone-naive or timezone-aware")
        if event_time > cutoff:
            raise ValueError(
                f"prediction cutoff cannot be earlier than match event time; cutoff is earlier than a match event at row {index}"
            )
    return values


def build_features(matches_path: Path, out_path: Path, prediction_cutoff=None, prediction_cutoffs=None):
    """Build pre-match features with explicit global or per-match cutoffs.

    When omitted, each match uses its own event time as the prediction point.
    A global cutoff applies to every row. A per-match mapping keyed by
    ``match_id`` applies each row's cutoff independently. In all modes,
    historical results still require strictly earlier event times.
    """
    df = pd.read_csv(matches_path, parse_dates=["match_date_utc"])
    resolved_cutoffs = _resolve_prediction_cutoffs(df, prediction_cutoff, prediction_cutoffs)
    if resolved_cutoffs is not None:
        df["prediction_cutoff"] = resolved_cutoffs
    elos = compute_elo_series(df)
    forms = compute_form_series(df)
    df["home_elo"] = [value[0] for value in elos]
    df["away_elo"] = [value[1] for value in elos]
    df["home_form_last3"] = [forms.get((index, row["home_team"]), np.nan) for index, row in df.iterrows()]
    df["away_form_last3"] = [forms.get((index, row["away_team"]), np.nan) for index, row in df.iterrows()]

    df = _sort_for_output(df)
    feature_cols = [
        "match_id", "match_date_utc", "home_team", "away_team",
        "home_elo", "away_elo", "home_form_last3", "away_form_last3",
    ]
    if resolved_cutoffs is not None:
        feature_cols.append("prediction_cutoff")
    feats = df[feature_cols].copy()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    feats.to_parquet(out_path, index=False)
    logging.info("Wrote features to %s", out_path)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--matches", default="data/processed/matches_canonical.csv")
    parser.add_argument("--out", default="data/processed/features_batch.parquet")
    parser.add_argument("--prediction-cutoff", default=None)
    args = parser.parse_args()
    build_features(Path(args.matches), Path(args.out), prediction_cutoff=args.prediction_cutoff)
