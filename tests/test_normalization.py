from src.normalization.team_aliases import to_canonical


def test_known_aliases():
    assert to_canonical("Man United") == "Manchester United"
    assert to_canonical("Man City") == "Manchester City"


def test_canonical_self():
    assert to_canonical("Manchester United") == "Manchester United"


def test_unknown_name_unchanged():
    assert to_canonical("Some Unknown FC") == "Some Unknown FC"


def test_empty_or_none():
    assert to_canonical("") == ""
    assert to_canonical(None) is None
