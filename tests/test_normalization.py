from src.normalization.team_aliases import team_identity_evidence, to_canonical


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


def test_team_identity_evidence_versions_suffix_based_alias_resolution():
    evidence = team_identity_evidence("Manchester Utd FC", strip_fc_suffix=True)

    assert evidence["resolved_name"] == "Manchester United"
    assert evidence["resolution_status"] == "mapped_with_versioned_aliases"
    assert evidence["resolution_rule"] == "unchanged_unmapped_name+strip_fc_suffix"
    assert evidence["mapping_version"] == "team-aliases-v1"
    assert len(evidence["mapping_sha256"]) == 64
