import pandas as pd
from pathlib import Path
from src.features.feature_builder import compute_elo_series, build_features


def make_sample_matches(tmp_path: Path):
    # create three historical matches, target match, and a future match
    rows = [
        {"match_id": "m1", "league": "l", "season": "s", "match_date_utc": "2023-01-01T12:00:00", "home_team": "A", "away_team": "B", "home_goals": 1, "away_goals": 0},
        {"match_id": "m2", "league": "l", "season": "s", "match_date_utc": "2023-01-05T12:00:00", "home_team": "B", "away_team": "C", "home_goals": 0, "away_goals": 2},
        {"match_id": "m3", "league": "l", "season": "s", "match_date_utc": "2023-01-10T12:00:00", "home_team": "C", "away_team": "A", "home_goals": 1, "away_goals": 1},
        # target match
        {"match_id": "m4", "league": "l", "season": "s", "match_date_utc": "2023-01-20T12:00:00", "home_team": "A", "away_team": "C", "home_goals": 2, "away_goals": 1},
        # future match
        {"match_id": "m5", "league": "l", "season": "s", "match_date_utc": "2023-02-01T12:00:00", "home_team": "C", "away_team": "B", "home_goals": 3, "away_goals": 0},
    ]
    df = pd.DataFrame(rows)
    path = tmp_path / "matches.csv"
    df.to_csv(path, index=False)
    return path


def test_no_future_leakage(tmp_path: Path):
    path = make_sample_matches(tmp_path)
    out = tmp_path / "features_all.parquet"
    # build features with future match present
    build_features(path, out)
    feats_all = pd.read_parquet(out)

    # remove future match and rebuild
    df = pd.read_csv(path)
    df2 = df[df["match_id"] != "m5"]
    path2 = tmp_path / "matches2.csv"
    df2.to_csv(path2, index=False)
    out2 = tmp_path / "features_nofuture.parquet"
    build_features(path2, out2)
    feats_nofut = pd.read_parquet(out2)

    # features for target match m4 should be identical
    a = feats_all[feats_all["match_id"] == "m4"].to_dict(orient="records")
    b = feats_nofut[feats_nofut["match_id"] == "m4"].to_dict(orient="records")
    assert len(a) == 1 and len(b) == 1
    assert a[0]["home_elo"] == b[0]["home_elo"]
    assert a[0]["away_elo"] == b[0]["away_elo"]
