from src.utils.odds import implied_prob, normalize_implied_probs


def test_implied_prob_valid():
    assert abs(implied_prob(2.0) - 0.5) < 1e-9


def test_implied_prob_invalid():
    assert implied_prob(0) is None
    assert implied_prob(-1) is None


def test_normalize_overround():
    probs = [0.5, 0.4, None]
    norm = normalize_implied_probs(probs)
    assert abs(sum([p for p in norm if p is not None]) - 1.0) < 1e-9
