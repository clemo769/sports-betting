"""Odds conversion utilities."""
import math

def implied_prob(odds):
    try:
        o = float(odds)
        if o <= 0 or math.isinf(o) or math.isnan(o):
            return None
        return 1.0 / o
    except Exception:
        return None

def normalize_implied_probs(probs):
    """Normalize implied probabilities to remove overround.

    Input: iterable of implied probs (floats or None)
    Returns: list of normalized probs (None preserved when input None)
    """
    vals = [p for p in probs if p is not None]
    s = sum(vals)
    if s == 0:
        return [None if p is None else 0.0 for p in probs]
    return [(p / s) if p is not None else None for p in probs]
