"""Feature engineering utilities (soccer-focused initial functions).

This file contains placeholder functions to compute team Elo and build a
feature vector from events. These are minimal and should be extended.
"""
import math
from typing import Dict, Any


def compute_team_elo(rating_a: float, rating_b: float, score_a: float, k: float = 20.0) -> float:
    """Update Elo rating for team A given an outcome.

    score_a: 1.0 for win, 0.5 draw, 0 loss
    """
    expected_a = 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))
    new_a = rating_a + k * (score_a - expected_a)
    return new_a


def build_basic_features(match_meta: Dict[str, Any], events: list) -> Dict[str, Any]:
    """Construct a minimal feature dict for a match using metadata and events.

    Returns a flat dict of features.
    """
    features = {}
    features["home_team_id"] = match_meta.get("home_team_id")
    features["away_team_id"] = match_meta.get("away_team_id")
    features["home_goals_so_far"] = 0
    features["away_goals_so_far"] = 0
    for ev in events:
        if ev.get("event_type") == "goal":
            if ev.get("team_id") == match_meta.get("home_team_id"):
                features["home_goals_so_far"] += 1
            else:
                features["away_goals_so_far"] += 1

    # Example derived feature
    features["goal_diff"] = features["home_goals_so_far"] - features["away_goals_so_far"]
    return features
