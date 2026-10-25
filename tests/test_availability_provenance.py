from datetime import datetime, timezone

import pandas as pd
import pytest

from scripts.fetch_openfootball import source_snapshot_metadata
from scripts.parse_fdco_csv import parse
from src.features.temporal_contract import availability_status, is_available


def test_openfootball_snapshot_timestamp_is_preserved(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{
                "sha": "abc123",
                "commit": {"committer": {"date": "2025-03-27T19:52:42Z"}},
            }]

    monkeypatch.setattr("scripts.fetch_openfootball.requests.get", lambda *args, **kwargs: Response())
    assert source_snapshot_metadata("2023-24", "premier-league") == {
        "source_available_ts": "2025-03-27T19:52:42Z",
        "source_available_ref": "abc123",
    }


def test_openfootball_snapshot_url_can_be_pinned():
    from scripts.fetch_openfootball import build_url

    assert build_url("2023-24", "premier-league", "abc123") == (
        "https://raw.githubusercontent.com/openfootball/football.json/abc123/2023-24/en.1.json"
    )


def test_availability_at_cutoff_is_allowed_and_after_is_not():
    cutoff = datetime(2025, 3, 27, 19, 52, 42, tzinfo=timezone.utc)
    assert is_available(cutoff, cutoff)
    assert availability_status(cutoff, cutoff) == "available"
    assert availability_status(datetime(2025, 3, 27, 19, 52, 43, tzinfo=timezone.utc), cutoff) == "unavailable"


def test_ingest_and_event_time_do_not_substitute_for_availability():
    cutoff = datetime(2024, 1, 10, tzinfo=timezone.utc)
    record = {
        "source_event_ts": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "source_available_ts": datetime(2024, 1, 11, tzinfo=timezone.utc),
        "ingest_ts": datetime(2024, 1, 2, tzinfo=timezone.utc),
    }
    assert availability_status(record["source_available_ts"], cutoff) == "unavailable"
    assert not is_available(record["source_available_ts"], cutoff)


def test_missing_availability_is_explicitly_unknown():
    cutoff = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert availability_status(None, cutoff) == "unknown_availability"
    assert availability_status(pd.NaT, cutoff) == "unknown_availability"


def test_fdco_parser_preserves_unknown_availability(tmp_path):
    source = tmp_path / "premier-league_2324.csv"
    pd.DataFrame([{
        "Date": "11/08/2023", "HomeTeam": "Burnley", "AwayTeam": "Man City",
        "FTHG": 0, "FTAG": 3,
    }]).to_csv(source, index=False)
    output = tmp_path / "processed"
    parse(source, output)
    parsed = pd.read_csv(output / "matches_canonical.csv")
    assert "source_available_ts" in parsed.columns
    assert parsed.loc[0, "source_available_ts"] != parsed.loc[0, "ingest_ts"]
    assert pd.isna(parsed.loc[0, "source_available_ts"])


def test_openfootball_timestamp_requires_commit_metadata(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    monkeypatch.setattr("scripts.fetch_openfootball.requests.get", lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match="No public commit timestamp"):
        source_snapshot_metadata("2023-24", "premier-league")