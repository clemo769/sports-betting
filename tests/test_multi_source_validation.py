import pandas as pd
import pytest

from scripts.validate_multi_source import compare_stable, run_validation


def row(source, source_id, home="Alpha", away="Beta", date="2024-01-01"):
    return {
        "source": source,
        "source_id": source_id,
        "league": "test-league",
        "season": "23/24",
        "home_team": home,
        "away_team": away,
        "match_date_utc": date,
    }


def test_same_inputs_have_identical_stable_linking_results():
    frame = pd.DataFrame([row("football-data.co.uk", "fdco-1"), row("openfootball", "of-1")])
    assert compare_stable(run_validation_results(frame), run_validation_results(frame)).empty


def run_validation_results(frame):
    from src.linking.match_linker import link_matches

    return link_matches(frame)


def test_ingest_timestamps_do_not_change_determinism():
    frame = pd.DataFrame([row("football-data.co.uk", "fdco-1"), row("openfootball", "of-1")])
    first = run_validation_results(frame)
    second = first.copy()
    second["ingest_ts"] = "a-different-run-time"
    assert compare_stable(first, second).empty


def test_genuine_stable_difference_is_reported():
    frame = pd.DataFrame([row("football-data.co.uk", "fdco-1"), row("openfootball", "of-1")])
    first = run_validation_results(frame)
    second = first.copy()
    second.loc[0, "matching_reason"] = "tampered"
    differences = compare_stable(first, second)
    assert len(differences) == 1


def test_fdco_and_openfootball_records_use_real_validation_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fdco_path = tmp_path / "fdco.csv"
    openfootball_path = tmp_path / "openfootball.csv"
    pd.DataFrame([row("football-data.co.uk", "fdco-1")]).to_csv(fdco_path, index=False)
    pd.DataFrame([row("openfootball", "of-1")]).to_csv(openfootball_path, index=False)

    linked, differences = run_validation(fdco_path, openfootball_path, tmp_path / "out")

    assert set(linked["source"]) == {"football-data.co.uk", "openfootball"}
    assert (linked["match_status"] == "matched").all()
    assert differences.empty
    assert (tmp_path / "out" / "multi_source_validation_summary.txt").exists()
    summary = (tmp_path / "out" / "multi_source_validation_summary.txt").read_text()
    assert "legitimate_multi_source_relationships: 1" in summary
    assert "actual_duplicate_source_records: 0" in summary
    assert "duplicate_canonical_id_count" not in summary


def test_validation_rejects_missing_required_columns(tmp_path):
    path = tmp_path / "bad.csv"
    pd.DataFrame([{"source": "football-data.co.uk"}]).to_csv(path, index=False)
    with pytest.raises(ValueError, match="missing required columns"):
        run_validation(path, path, tmp_path / "out")