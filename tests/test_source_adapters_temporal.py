from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from scripts.fetch_openfootball import fetch_matches
from scripts.parse_fdco_csv import parse as parse_fdco
from src.source_evidence import canonical_content_sha256
from src.temporal import resolve_local_event_time, resolve_venue_timezone


def test_premier_league_uses_historical_london_zone_with_explicit_venue_evidence():
    zone, basis = resolve_venue_timezone(
        league="premier-league",
        venue_name="Example Ground",
        venue_country="England",
    )

    assert zone == "Europe/London"
    assert basis == "premier_league_england_wales_venue"
    assert resolve_local_event_time(datetime(2024, 1, 15, 15, 0), zone) == datetime(
        2024, 1, 15, 15, 0, tzinfo=timezone.utc
    )
    assert resolve_local_event_time(datetime(2024, 7, 15, 15, 0), zone) == datetime(
        2024, 7, 15, 14, 0, tzinfo=timezone.utc
    )


def test_explicit_venue_timezone_overrides_premier_league_default():
    zone, basis = resolve_venue_timezone(
        league="premier-league",
        venue_name="Neutral Venue",
        venue_country="France",
        venue_timezone="Europe/Paris",
    )

    assert zone == "Europe/Paris"
    assert basis == "explicit_venue_timezone"
    assert resolve_local_event_time(datetime(2024, 1, 15, 20, 0), zone) == datetime(
        2024, 1, 15, 19, 0, tzinfo=timezone.utc
    )


def test_timezone_is_unresolved_without_venue_evidence():
    assert resolve_venue_timezone(league="premier-league", venue_name=None) == (
        None,
        "missing_venue",
    )
    assert resolve_venue_timezone(
        league="premier-league",
        venue_name="Unverified Ground",
    ) == (None, "missing_venue_timezone")
    assert resolve_venue_timezone(
        league="championship",
        venue_name="Ground",
        venue_country="England",
    ) == (None, "missing_venue_timezone")


def test_invalid_explicit_venue_timezone_is_unresolved():
    assert resolve_venue_timezone(
        league="premier-league",
        venue_name="Ground",
        venue_timezone="Mars/Olympus",
    ) == (None, "invalid_venue_timezone")


@pytest.mark.parametrize(
    ("local_time", "error"),
    [
        (datetime(2024, 3, 31, 1, 30), "nonexistent"),
        (datetime(2024, 10, 27, 1, 30), "ambiguous"),
    ],
)
def test_london_daylight_saving_gaps_and_folds_are_rejected(local_time, error):
    with pytest.raises(ValueError, match=error):
        resolve_local_event_time(local_time, "Europe/London")


def test_local_event_time_requires_naive_wall_time_and_known_iana_zone():
    with pytest.raises(ValueError, match="must be naive"):
        resolve_local_event_time(datetime(2024, 1, 1, tzinfo=timezone.utc), "Europe/London")
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        resolve_local_event_time(datetime(2024, 1, 1), "Mars/Olympus")


def _write_fdco(path: Path, *, date="15/01/2024", time="20:00"):
    pd.DataFrame([{
        "Date": date,
        "Time": time,
        "HomeTeam": "Arsenal",
        "AwayTeam": "Chelsea",
        "FTHG": "2",
        "FTAG": "0",
    }]).to_csv(path, index=False)


def test_fdco_preserves_raw_date_time_and_unknown_availability(tmp_path):
    source_path = tmp_path / "premier-league_2324.csv"
    _write_fdco(source_path, date="15/01/2024", time="20:05")

    result = parse_fdco(source_path, tmp_path / "processed")
    parsed = pd.read_csv(tmp_path / "processed" / "matches_canonical.csv")

    assert result["event_times_unresolved"] == 1
    assert parsed.loc[0, "source_event_date_raw"] == "15/01/2024"
    assert parsed.loc[0, "source_event_time_raw"] == "20:05"
    assert pd.isna(parsed.loc[0, "match_date_utc"])
    assert pd.isna(parsed.loc[0, "source_event_ts"])
    assert pd.isna(parsed.loc[0, "source_available_ts"])
    assert pd.isna(parsed.loc[0, "source_published_ts"])
    assert pd.isna(parsed.loc[0, "availability_basis"])
    assert pd.Timestamp(parsed.loc[0, "ingest_ts"]).utcoffset().total_seconds() == 0
    assert parsed.loc[0, "original_filename"] == source_path.name
    assert parsed.loc[0, "parser_version"] == "parse_fdco_csv_v2"

    raw_reference = json.loads(parsed.loc[0, "raw_reference"])
    assert raw_reference["source_event_date_raw"] == "15/01/2024"
    assert raw_reference["source_event_time_raw"] == "20:05"
    assert raw_reference["source_snapshot_id"].startswith("fdco:")
    assert len(raw_reference["source_snapshot_sha256"]) == 64
    evidence = raw_reference["source_row_evidence"]
    assert evidence["schema_version"] == "source-row-evidence-v1"
    assert evidence["legacy_source_id"] == parsed.loc[0, "source_id"]
    assert evidence["row"]["raw_content"]["values"] == [
        "15/01/2024", "20:05", "Arsenal", "Chelsea", "2", "0"
    ]
    assert evidence["row"]["content_sha256"] == canonical_content_sha256(
        evidence["row"]["raw_content"]
    )
    assert evidence["snapshot"]["snapshot_sha256"] == raw_reference["source_snapshot_sha256"]
    assert evidence["identity"]["provider_logical_record_id"] is None
    assert evidence["revision"]["ordering_status"] == "unavailable"
    assert evidence["availability"]["status"] == "unknown"
    assert parsed.loc[0, "snapshot_row_identity"] == evidence["identity"]["snapshot_scoped_row_id"]


def test_fdco_snapshot_row_identity_is_deterministic_and_keeps_duplicate_rows_distinct(tmp_path):
    source_path = tmp_path / "premier-league_2324.csv"
    rows = pd.DataFrame([
        {"Date": "15/01/2024", "Time": "20:00", "HomeTeam": "Arsenal", "AwayTeam": "Chelsea", "FTHG": "2", "FTAG": "0"},
        {"Date": "15/01/2024", "Time": "20:00", "HomeTeam": "Arsenal", "AwayTeam": "Chelsea", "FTHG": "2", "FTAG": "0"},
    ])
    rows.to_csv(source_path, index=False)

    parse_fdco(source_path, tmp_path / "first")
    parse_fdco(source_path, tmp_path / "second")
    first = pd.read_csv(tmp_path / "first" / "matches_canonical.csv")
    second = pd.read_csv(tmp_path / "second" / "matches_canonical.csv")

    first_ids = first["snapshot_row_identity"].tolist()
    assert first_ids == second["snapshot_row_identity"].tolist()
    assert first_ids[0] != first_ids[1]
    first_evidence = [json.loads(value)["source_row_evidence"] for value in first["raw_reference"]]
    assert first_evidence[0]["row"]["content_sha256"] == first_evidence[1]["row"]["content_sha256"]
    assert all(item["identity"]["provider_logical_record_id"] is None for item in first_evidence)


def test_fdco_row_reordering_does_not_establish_cross_snapshot_logical_identity(tmp_path):
    source_path = tmp_path / "premier-league_2324.csv"
    source_rows = [
        {"Date": "15/01/2024", "Time": "20:00", "HomeTeam": "Arsenal", "AwayTeam": "Chelsea", "FTHG": "2", "FTAG": "0"},
        {"Date": "16/01/2024", "Time": "20:00", "HomeTeam": "Burnley", "AwayTeam": "Everton", "FTHG": "1", "FTAG": "1"},
    ]
    pd.DataFrame(source_rows).to_csv(source_path, index=False)
    parse_fdco(source_path, tmp_path / "first")
    pd.DataFrame(list(reversed(source_rows))).to_csv(source_path, index=False)
    parse_fdco(source_path, tmp_path / "reordered")

    first = pd.read_csv(tmp_path / "first" / "matches_canonical.csv")
    reordered = pd.read_csv(tmp_path / "reordered" / "matches_canonical.csv")
    first_arsenal = next(
        json.loads(value)["source_row_evidence"]
        for value in first["raw_reference"]
        if json.loads(value)["source_row_evidence"]["row"]["raw_content"]["values"][2] == "Arsenal"
    )
    reordered_arsenal = next(
        json.loads(value)["source_row_evidence"]
        for value in reordered["raw_reference"]
        if json.loads(value)["source_row_evidence"]["row"]["raw_content"]["values"][2] == "Arsenal"
    )

    assert first_arsenal["identity"]["snapshot_scoped_row_id"] != reordered_arsenal["identity"]["snapshot_scoped_row_id"]
    assert first_arsenal["identity"]["provider_logical_record_id"] is None
    assert reordered_arsenal["identity"]["provider_logical_record_id"] is None


def test_fdco_content_and_snapshot_hashes_are_independent(tmp_path):
    source_path = tmp_path / "premier-league_2324.csv"
    _write_fdco(source_path)
    parse_fdco(source_path, tmp_path / "first")
    first = pd.read_csv(tmp_path / "first" / "matches_canonical.csv").iloc[0]
    first_evidence = json.loads(first["raw_reference"])["source_row_evidence"]

    _write_fdco(source_path, time="20:05")
    parse_fdco(source_path, tmp_path / "changed")
    changed = pd.read_csv(tmp_path / "changed" / "matches_canonical.csv").iloc[0]
    changed_evidence = json.loads(changed["raw_reference"])["source_row_evidence"]

    assert first_evidence["row"]["content_sha256"] != changed_evidence["row"]["content_sha256"]
    assert first_evidence["snapshot"]["snapshot_sha256"] != changed_evidence["snapshot"]["snapshot_sha256"]
    assert first_evidence["identity"]["snapshot_scoped_row_id"] != changed_evidence["identity"]["snapshot_scoped_row_id"]


def test_fdco_resolves_local_time_only_with_explicit_venue_evidence(tmp_path):
    source_path = tmp_path / "premier-league_2324.csv"
    _write_fdco(source_path, date="15/01/2024", time="20:00")
    metadata = {
        f"{source_path.name}:0": {
            "venue_name": "Verified Ground",
            "venue_country": "England",
        }
    }

    parse_fdco(source_path, tmp_path / "processed", venue_metadata=metadata)
    parsed = pd.read_csv(tmp_path / "processed" / "matches_canonical.csv")

    assert parsed.loc[0, "venue_timezone"] == "Europe/London"
    assert parsed.loc[0, "timezone_resolution_basis"] == "premier_league_england_wales_venue"
    assert parsed.loc[0, "source_event_ts"] == "2024-01-15T20:00:00+00:00"
    assert parsed.loc[0, "match_date_utc"] == parsed.loc[0, "source_event_ts"]


def test_fdco_supports_neutral_venue_timezone_override(tmp_path):
    source_path = tmp_path / "premier-league_2324.csv"
    _write_fdco(source_path, date="15/01/2024", time="20:00")
    metadata = {
        f"{source_path.name}:0": {
            "venue_name": "Neutral Stadium",
            "venue_country": "France",
            "venue_timezone": "Europe/Paris",
        }
    }

    parse_fdco(source_path, tmp_path / "processed", venue_metadata=metadata)
    parsed = pd.read_csv(tmp_path / "processed" / "matches_canonical.csv")

    assert parsed.loc[0, "venue_timezone"] == "Europe/Paris"
    assert parsed.loc[0, "source_event_ts"] == "2024-01-15T19:00:00+00:00"


@pytest.mark.parametrize(
    ("date", "time", "metadata", "expected_reason"),
    [
        ("15/01/2024", "", {"venue_name": "Ground", "venue_country": "England"}, "missing_or_invalid_date_time"),
        ("15/01/2024", "20:00", None, "missing_venue"),
        ("31/03/2024", "01:30", {"venue_name": "Ground", "venue_country": "England"}, "nonexistent"),
        ("27/10/2024", "01:30", {"venue_name": "Ground", "venue_country": "England"}, "ambiguous"),
    ],
)
def test_fdco_unresolved_times_are_quarantined(tmp_path, date, time, metadata, expected_reason):
    source_path = tmp_path / "premier-league_2324.csv"
    _write_fdco(source_path, date=date, time=time)
    venue_map = {f"{source_path.name}:0": metadata} if metadata else None
    outdir = tmp_path / "processed"

    result = parse_fdco(source_path, outdir, venue_metadata=venue_map)
    parsed = pd.read_csv(outdir / "matches_canonical.csv")
    quarantined = pd.read_csv(outdir / "unresolved_event_times.csv")

    assert result["event_times_unresolved"] == 1
    assert pd.isna(parsed.loc[0, "source_event_ts"])
    assert expected_reason in quarantined.loc[0, "resolution_error"]


def _openfootball_responses(commit_sha="abc123"):
    payload = {
        "matches": [
            {
                "date": "2024-01-15",
                "time": "20:00",
                "team1": "Arsenal",
                "team2": "Chelsea",
                "venue": {"name": "Test Ground", "country": "England"},
                "score": {"ft": [2, 0]},
            },
            {
                "date": "2024-01-16",
                "time": "20:00",
                "team1": "Burnley",
                "team2": "Everton",
                "score": {"ft": [None, None]},
            },
        ]
    }
    snapshot_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    class Response:
        def __init__(self, value, content=b""):
            self.value = value
            self.content = content

        def raise_for_status(self):
            return None

        def json(self):
            return self.value

    def get(url, **kwargs):
        if "api.github.com" in url:
            return Response([{
                "sha": commit_sha,
                "commit": {"committer": {"date": "2025-03-27T19:52:42Z"}},
            }])
        return Response(payload, snapshot_bytes)

    return get, snapshot_bytes


def test_openfootball_preserves_raw_event_and_snapshot_provenance(monkeypatch, tmp_path):
    mock_get, snapshot_bytes = _openfootball_responses()
    monkeypatch.setattr("scripts.fetch_openfootball.requests.get", mock_get)

    output = fetch_matches("2023-24", "premier-league", tmp_path)
    parsed = pd.read_csv(output)
    first = parsed.iloc[0]
    second = parsed.iloc[1]

    assert first["source_event_date_raw"] == "2024-01-15"
    assert first["source_event_time_raw"] == "20:00"
    assert first["venue_name"] == "Test Ground"
    assert first["venue_timezone"] == "Europe/London"
    assert first["source_event_ts"] == "2024-01-15T20:00:00+00:00"
    assert pd.isna(first["source_published_ts"])
    assert first["availability_basis"] == "snapshot"
    assert first["snapshot_available_at"] == "2025-03-27T19:52:42+00:00"
    assert first["source_available_ts"] == first["snapshot_available_at"]
    assert first["source_available_ref"] == "abc123"
    assert first["source_file_path"] == "2023-24/en.1.json"
    assert first["source_snapshot_id"] == (
        "github:openfootball/football.json:2023-24/en.1.json@abc123"
    )
    assert first["source_snapshot_sha256"] == hashlib.sha256(snapshot_bytes).hexdigest()
    assert pd.isna(second["match_date_utc"])
    assert pd.isna(second["source_event_ts"])
    unresolved_path = tmp_path / "unresolved_event_times_openfootball_premier-league_202324.csv"
    assert len(pd.read_csv(unresolved_path)) == 1

    raw_reference = json.loads(first["raw_reference"])
    assert raw_reference["source_commit_sha"] == "abc123"
    assert raw_reference["source_snapshot_id"] == first["source_snapshot_id"]
    assert raw_reference["source_event_date_raw"] == "2024-01-15"
    assert raw_reference["source_event_time_raw"] == "20:00"
    evidence = raw_reference["source_row_evidence"]
    assert evidence["row"]["raw_content"] == json.loads(snapshot_bytes)["matches"][0]
    assert evidence["row"]["content_sha256"] == canonical_content_sha256(evidence["row"]["raw_content"])
    assert evidence["snapshot"]["source_repository"] == "openfootball/football.json"
    assert evidence["snapshot"]["source_file_path"] == "2023-24/en.1.json"
    assert evidence["availability"]["available_ref"] == "abc123"
    assert evidence["availability"]["provider_published_at"] is None
    assert evidence["availability"]["status"] == "snapshot_available"
    assert evidence["revision"]["ordering_status"] == "unavailable"
    assert evidence["identity"]["provider_logical_record_id"] is None


def test_openfootball_same_pinned_snapshot_has_deterministic_source_identity(monkeypatch, tmp_path):
    mock_get, _ = _openfootball_responses(commit_sha="commit-fixed")
    monkeypatch.setattr("scripts.fetch_openfootball.requests.get", mock_get)

    first_path = fetch_matches("2023-24", "premier-league", tmp_path / "first")
    second_path = fetch_matches("2023-24", "premier-league", tmp_path / "second")
    first = pd.read_csv(first_path)
    second = pd.read_csv(second_path)

    for column in (
        "source_id",
        "match_id",
        "source_snapshot_id",
        "source_snapshot_sha256",
        "snapshot_row_identity",
    ):
        assert first[column].tolist() == second[column].tolist()


def test_adapter_rows_preserve_evidence_through_linked_record_persistence(monkeypatch, tmp_path):
    from src.db.ingestion import persist_linked_records
    from src.db.models import ResultObservation

    class FakeSession:
        def __init__(self):
            self.added = []
            self.flushed = False

        def get(self, model, identity):
            return None

        def scalar(self, statement):
            return None

        def scalars(self, statement):
            return []

        def add(self, item):
            self.added.append(item)

        def flush(self):
            self.flushed = True

    fdco_path = tmp_path / "premier-league_2324.csv"
    _write_fdco(fdco_path)
    venue_metadata = {
        f"{fdco_path.name}:0": {
            "venue_name": "Verified Ground",
            "venue_country": "England",
        }
    }
    fdco_output = tmp_path / "fdco"
    parse_fdco(fdco_path, fdco_output, venue_metadata=venue_metadata)

    openfootball_get, _ = _openfootball_responses()
    monkeypatch.setattr("scripts.fetch_openfootball.requests.get", openfootball_get)
    openfootball_path = fetch_matches("2023-24", "premier-league", tmp_path / "openfootball")

    fdco_row = pd.read_csv(fdco_output / "matches_canonical.csv").iloc[0].to_dict()
    openfootball_row = pd.read_csv(openfootball_path).iloc[0].to_dict()
    match_id = uuid4()
    linked_records = []
    for row in (fdco_row, openfootball_row):
        row.update({
            "canonical_match_id": str(match_id),
            "match_status": "matched",
            "league": "premier-league",
            "season": "2324",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_goals": 2,
            "away_goals": 0,
        })
        linked_records.append(row)

    session = FakeSession()
    persist_linked_records(
        session,
        linked_records,
        {str(match_id): datetime(2024, 1, 15, 20, 0, tzinfo=timezone.utc)},
    )

    source_records = [item for item in session.added if item.__class__.__name__ == "SourceRecord"]
    result_observations = [item for item in session.added if isinstance(item, ResultObservation)]
    assert len(source_records) == 2
    assert len(result_observations) == 2
    assert session.flushed
    persisted_fdco = next(item for item in source_records if item.source == "football-data.co.uk")
    persisted_openfootball = next(item for item in source_records if item.source == "openfootball")
    fdco_raw_reference = json.loads(persisted_fdco.raw_reference)
    openfootball_raw_reference = json.loads(persisted_openfootball.raw_reference)

    assert persisted_fdco.source_available_ts is None
    assert persisted_fdco.source_published_ts is None
    assert persisted_fdco.availability_basis is None
    assert fdco_raw_reference["source_event_date_raw"] == "15/01/2024"
    assert fdco_raw_reference["source_event_time_raw"] == "20:00"
    assert fdco_raw_reference["source_snapshot_sha256"] == fdco_row["source_snapshot_sha256"]

    assert persisted_openfootball.source_published_ts is None
    assert persisted_openfootball.first_observed_at is None
    assert persisted_openfootball.availability_basis == "snapshot"
    assert persisted_openfootball.source_available_ts == persisted_openfootball.snapshot_available_at
    assert persisted_openfootball.source_available_ref == "abc123"
    assert openfootball_raw_reference["source_repository"] == "openfootball/football.json"
    assert openfootball_raw_reference["source_file_path"] == "2023-24/en.1.json"
    assert openfootball_raw_reference["source_commit_sha"] == "abc123"
    assert openfootball_raw_reference["source_snapshot_id"] == openfootball_row["source_snapshot_id"]
    assert openfootball_raw_reference["snapshot_sha256"] == openfootball_row["source_snapshot_sha256"]
