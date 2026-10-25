"""Lightweight feature metadata for the time-safe feature pipeline."""

PREDICTION_CUTOFF_METADATA = {
    "field": "prediction_cutoff",
    "representation": "match_id-keyed mapping resolved onto each feature row",
    "rule": "availability_time <= prediction_cutoff",
    "event_result_rule": "historical event time < current event time",
    "ingest_time_is_availability": False,
}

SOURCE_AVAILABILITY_METADATA = {
    "openfootball": {
        "field": "source_available_ts",
        "meaning": "GitHub committer timestamp for the exact public season-file snapshot",
        "does_not_prove": "original publication time of each match datum",
    },
    "football-data.co.uk": {
        "field": "source_available_ts",
        "meaning": "unknown; source does not provide a row-level publication/update timestamp",
        "does_not_prove": "availability from ingest_ts or source_event_ts",
    },
}

FEATURES = [
    {
        "name": "home_elo",
        "definition": "Pre-match Elo rating for home team (initialized at 1500).",
        "source": "matches_canonical.csv",
        "calculation": "iterative Elo updated after each finished match with K=20",
        "lookback": "full history",
        "time_dependent": True,
        "availability_rule": "historical event time < current event time",
        "uses_match_result": True,
        "requires_publication_timestamp": False,
        "missing_behavior": "no missing once computed; if team unseen, starts at 1500",
    },
    {
        "name": "away_elo",
        "definition": "Pre-match Elo rating for away team.",
        "source": "matches_canonical.csv",
        "calculation": "iterative Elo updated after each finished match with K=20",
        "lookback": "full history",
        "time_dependent": True,
        "availability_rule": "historical event time < current event time",
        "uses_match_result": True,
        "requires_publication_timestamp": False,
        "missing_behavior": "no missing once computed; if team unseen, starts at 1500",
    },
    {
        "name": "home_form_last3",
        "definition": "Mean points per match for home team over last 3 completed matches (shifted to exclude current match).",
        "source": "matches_canonical.csv",
        "calculation": "rolling mean of last 3 points (3/1/0) across home+away matches, shifted by 1",
        "lookback": "3 matches",
        "time_dependent": True,
        "availability_rule": "three completed historical matches with event time < current event time",
        "uses_match_result": True,
        "requires_publication_timestamp": False,
        "missing_behavior": "NaN until team has 3 prior matches",
    },
    {
        "name": "away_form_last3",
        "definition": "Mean points per match for away team over last 3 completed matches (shifted).",
        "source": "matches_canonical.csv",
        "calculation": "rolling mean of last 3 points (3/1/0) across home+away matches, shifted by 1",
        "lookback": "3 matches",
        "time_dependent": True,
        "availability_rule": "three completed historical matches with event time < current event time",
        "uses_match_result": True,
        "requires_publication_timestamp": False,
        "missing_behavior": "NaN until team has 3 prior matches",
    },
]
