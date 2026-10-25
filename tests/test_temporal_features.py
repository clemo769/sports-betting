from datetime import datetime, timezone

import pandas as pd
import pytest

from src.features.feature_builder import build_features, compute_elo_series, compute_form_series
from src.features.temporal_contract import is_available, validate_prediction_cutoff
from src.models.train_baseline import prepare_training_data


def matches():
    return pd.DataFrame([
        {"match_id": "m1", "match_date_utc": "2024-01-01T12:00:00Z", "home_team": "B", "away_team": "A", "home_goals": 1, "away_goals": 0},
        {"match_id": "m2", "match_date_utc": "2024-01-05T12:00:00Z", "home_team": "A", "away_team": "C", "home_goals": 0, "away_goals": 1},
        {"match_id": "m3", "match_date_utc": "2024-01-10T12:00:00Z", "home_team": "D", "away_team": "A", "home_goals": 1, "away_goals": 1},
        {"match_id": "m4", "match_date_utc": "2024-01-20T12:00:00Z", "home_team": "A", "away_team": "B", "home_goals": 2, "away_goals": 0},
        {"match_id": "m5", "match_date_utc": "2024-02-01T12:00:00Z", "home_team": "A", "away_team": "C", "home_goals": 0, "away_goals": 5},
    ])


def by_id(values, frame):
    return {row.match_id: value for row, value in zip(frame.itertuples(), values)}


def test_elo_excludes_current_result_and_future_results():
    frame = matches()
    baseline = by_id(compute_elo_series(frame), frame)
    changed_current = frame.copy()
    changed_current.loc[changed_current["match_id"] == "m4", ["home_goals", "away_goals"]] = [0, 5]
    changed_future = frame.copy()
    changed_future.loc[changed_future["match_id"] == "m5", ["home_goals", "away_goals"]] = [5, 0]

    assert baseline["m4"] == by_id(compute_elo_series(changed_current), frame)["m4"]
    assert baseline["m4"] == by_id(compute_elo_series(changed_future), frame)["m4"]


def test_form_excludes_current_and_future_results():
    frame = matches()
    baseline = compute_form_series(frame)
    changed = frame.copy()
    changed.loc[changed["match_id"] == "m4", ["home_goals", "away_goals"]] = [0, 5]
    changed.loc[changed["match_id"] == "m5", ["home_goals", "away_goals"]] = [5, 0]
    changed_form = compute_form_series(changed)
    m4_index = frame.index[frame["match_id"] == "m4"][0]
    assert baseline[(m4_index, "A")] == changed_form[(m4_index, "A")] == 1 / 3


def test_exact_last_three_form_fixture():
    frame = matches()
    form = compute_form_series(frame)
    m4_index = frame.index[frame["match_id"] == "m4"][0]
    assert form[(m4_index, "A")] == 1 / 3


def test_same_timestamp_results_do_not_cross_contaminate_or_depend_on_input_order():
    frame = pd.DataFrame([
        {"match_id": "a", "match_date_utc": "2024-01-01T12:00:00Z", "home_team": "A", "away_team": "B", "home_goals": 5, "away_goals": 0},
        {"match_id": "b", "match_date_utc": "2024-01-01T12:00:00Z", "home_team": "B", "away_team": "C", "home_goals": 0, "away_goals": 5},
    ])
    first = by_id(compute_elo_series(frame), frame)
    reordered = frame.iloc[::-1].reset_index(drop=True)
    second = by_id(compute_elo_series(reordered), reordered)
    assert first == second
    assert first["b"] == (1500.0, 1500.0)


def test_training_labels_stay_with_feature_match_after_sorting(tmp_path):
    features = pd.DataFrame([
        {"match_id": "late", "match_date_utc": "2024-02-01", "home_elo": 2},
        {"match_id": "early", "match_date_utc": "2024-01-01", "home_elo": 1},
    ])
    labels = pd.DataFrame([
        {"match_id": "late", "market": "O/U_2.5", "label_value": 0},
        {"match_id": "early", "market": "O/U_2.5", "label_value": 1},
    ])
    features_path = tmp_path / "features.parquet"
    labels_path = tmp_path / "labels.csv"
    features.to_parquet(features_path, index=False)
    labels.to_csv(labels_path, index=False)
    prepared = prepare_training_data(features_path, labels_path)
    assert prepared[["match_id", "home_elo", "label_value"]].to_dict("records") == [
        {"match_id": "early", "home_elo": 1, "label_value": 1},
        {"match_id": "late", "home_elo": 2, "label_value": 0},
    ]


def test_prediction_cutoff_requires_known_availability():
    cutoff = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert is_available(datetime(2023, 12, 31, tzinfo=timezone.utc), cutoff)
    assert not is_available(datetime(2024, 1, 2, tzinfo=timezone.utc), cutoff)
    assert not is_available(None, cutoff)


def test_build_features_accepts_explicit_cutoff(tmp_path):
    path = tmp_path / "matches.csv"
    matches().to_csv(path, index=False)
    output = tmp_path / "features.parquet"
    build_features(path, output, prediction_cutoff="2024-02-01T12:00:00Z")
    assert output.exists()


def test_build_features_accepts_distinct_cutoffs_per_match(tmp_path):
    path = tmp_path / "matches.csv"
    matches().to_csv(path, index=False)
    output = tmp_path / "features.parquet"
    cutoffs = {
        "m1": "2024-01-01T12:00:00Z",
        "m2": "2024-01-05T12:00:00Z",
        "m3": "2024-01-10T12:00:00Z",
        "m4": "2024-01-21T12:00:00Z",
        "m5": "2024-02-01T12:00:00Z",
    }
    build_features(path, output, prediction_cutoffs=cutoffs)
    result = pd.read_parquet(output).set_index("match_id")
    assert result.loc["m1", "prediction_cutoff"] == pd.Timestamp(cutoffs["m1"])
    assert result.loc["m4", "prediction_cutoff"] == pd.Timestamp(cutoffs["m4"])
    assert result.loc["m5", "prediction_cutoff"] == pd.Timestamp(cutoffs["m5"])


def test_per_match_cutoff_equality_is_available_for_each_row():
    frame = matches()
    cutoffs = {row.match_id: pd.Timestamp(row.match_date_utc) for row in frame.itertuples()}
    assert all(is_available(cutoffs[match_id], cutoffs[match_id]) for match_id in cutoffs)


def test_per_match_cutoffs_do_not_use_ingest_time_as_availability():
    cutoff_a = datetime(2024, 1, 10, tzinfo=timezone.utc)
    cutoff_b = datetime(2024, 1, 20, tzinfo=timezone.utc)
    availability = datetime(2024, 1, 15, tzinfo=timezone.utc)
    assert not is_available(availability, cutoff_a)
    assert is_available(availability, cutoff_b)


def test_per_match_cutoffs_reject_missing_invalid_and_mixed_timezones(tmp_path):
    path = tmp_path / "matches.csv"
    matches().to_csv(path, index=False)
    output = tmp_path / "features.parquet"
    complete = {f"m{i}": "2024-03-01T00:00:00Z" for i in range(1, 6)}
    with pytest.raises(ValueError, match="missing match_id"):
        build_features(path, output, prediction_cutoffs={"m1": complete["m1"]})
    invalid = complete.copy()
    invalid["m3"] = "invalid"
    with pytest.raises(ValueError, match="valid timestamp"):
        build_features(path, output, prediction_cutoffs=invalid)
    mixed = complete.copy()
    mixed["m2"] = "2024-03-01T00:00:00"
    with pytest.raises(ValueError, match="timezone-naive or timezone-aware"):
        build_features(path, output, prediction_cutoffs=mixed)


def test_per_match_cutoffs_reject_cutoff_before_its_own_event(tmp_path):
    path = tmp_path / "matches.csv"
    matches().to_csv(path, index=False)
    output = tmp_path / "features.parquet"
    cutoffs = {f"m{i}": "2024-03-01T00:00:00Z" for i in range(1, 6)}
    cutoffs["m4"] = "2024-01-19T00:00:00Z"
    with pytest.raises(ValueError, match="earlier than match event"):
        build_features(path, output, prediction_cutoffs=cutoffs)


def test_global_and_historical_cutoff_modes_remain_supported(tmp_path):
    path = tmp_path / "matches.csv"
    matches().to_csv(path, index=False)
    global_output = tmp_path / "global.parquet"
    historical_output = tmp_path / "historical.parquet"
    build_features(path, global_output, prediction_cutoff="2024-02-01T12:00:00Z")
    build_features(path, historical_output)
    assert "prediction_cutoff" in pd.read_parquet(global_output).columns
    assert "prediction_cutoff" not in pd.read_parquet(historical_output).columns


def test_availability_at_cutoff_is_allowed():
    cutoff = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert is_available(cutoff, cutoff)


def test_future_availability_cannot_be_substituted_by_ingest_time():
    cutoff = datetime(2024, 1, 1, tzinfo=timezone.utc)
    record = {"availability_time": datetime(2024, 1, 2, tzinfo=timezone.utc), "ingest_ts": cutoff}
    assert not is_available(record["availability_time"], cutoff)


def test_build_features_rejects_invalid_or_impossible_cutoff(tmp_path):
    path = tmp_path / "matches.csv"
    matches().to_csv(path, index=False)
    output = tmp_path / "features.parquet"
    with pytest.raises(ValueError, match="valid timestamp"):
        validate_prediction_cutoff("not-a-time")
    with pytest.raises(ValueError, match="earlier than a match event"):
        build_features(path, output, prediction_cutoff="2023-12-31T23:59:59Z")
    with pytest.raises(ValueError, match="timezone-naive or timezone-aware"):
        build_features(path, output, prediction_cutoff="2024-02-01T12:00:00")