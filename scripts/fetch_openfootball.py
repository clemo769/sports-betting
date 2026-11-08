"""Fetch OpenFootball football.json data into the canonical match schema."""
import argparse
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping

import pandas as pd
import requests

from src.normalization.team_aliases import team_identity_evidence
from src.source_evidence import build_source_row_evidence
from src.temporal import (
    load_venue_metadata,
    resolve_local_event_time,
    resolve_venue_timezone,
)

LOG = logging.getLogger("openfootball")

LEAGUE_FILES = {
    "premier-league": "en.1.json",
}


def source_snapshot_metadata(season: str, league: str) -> dict:
    """Return the public repository update timestamp for the source file.

    This is the GitHub commit time for the file snapshot, not the match event
    time and not proof of when the underlying data first became public.
    """
    path = f"{season}/{LEAGUE_FILES[league]}"
    response = requests.get(
        "https://api.github.com/repos/openfootball/football.json/commits",
        params={"path": path, "per_page": 1},
        headers={"Accept": "application/vnd.github+json", "User-Agent": "sports-predictions/0.1"},
        timeout=30,
    )
    response.raise_for_status()
    commits = response.json()
    if not commits or not commits[0].get("commit", {}).get("committer", {}).get("date"):
        raise RuntimeError(f"No public commit timestamp found for OpenFootball file {path}")
    commit = commits[0]
    commit_timestamp = datetime.fromisoformat(
        commit["commit"]["committer"]["date"].replace("Z", "+00:00")
    )
    if commit_timestamp.tzinfo is None or commit_timestamp.utcoffset() is None:
        raise RuntimeError(f"OpenFootball commit timestamp for {path} is not timezone-aware")
    commit_timestamp = commit_timestamp.astimezone(timezone.utc)
    commit_sha = commit.get("sha")
    if not isinstance(commit_sha, str) or not commit_sha.strip():
        raise RuntimeError(f"No commit SHA found for OpenFootball file {path}")
    repository = "openfootball/football.json"
    return {
        "source_available_ts": commit_timestamp.isoformat(),
        "source_available_ref": commit_sha,
        "source_published_ts": None,
        "snapshot_available_at": commit_timestamp.isoformat(),
        "availability_basis": "snapshot",
        "source_repository": repository,
        "source_file_path": path,
        "source_snapshot_id": f"github:{repository}:{path}@{commit_sha}",
        "source_commit_sha": commit_sha,
    }


def build_url(season: str, league: str, ref: str | None = None) -> str:
    if league not in LEAGUE_FILES:
        raise ValueError(f"Unsupported OpenFootball league: {league}")
    revision = ref or "master"
    return f"https://raw.githubusercontent.com/openfootball/football.json/{revision}/{season}/{LEAGUE_FILES[league]}"


def fdco_season(season: str) -> str:
    start_year, end_year = season.split("-", 1)
    return f"{start_year[-2:]}{end_year[-2:]}"


def canonical_team(name):
    return team_identity_evidence(name, strip_fc_suffix=True)["resolved_name"]


def _venue_value(match: dict, key_names: tuple[str, ...]) -> str | None:
    venue = match.get("venue")
    for key in key_names:
        value = match.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        if isinstance(venue, dict):
            value = venue.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    if isinstance(venue, str) and "name" in key_names and venue.strip():
        return venue.strip()
    return None


def fetch_matches(
    season: str,
    league: str,
    outdir: Path,
    venue_metadata: Mapping[str, Mapping[str, str]] | None = None,
) -> Path:
    snapshot = source_snapshot_metadata(season, league)
    url = build_url(season, league, snapshot["source_available_ref"])
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    snapshot_bytes = response.content
    payload = response.json()
    matches = payload.get("matches")
    if not isinstance(matches, list):
        raise RuntimeError("OpenFootball response does not contain a matches list")

    ingest_ts = datetime.now(timezone.utc).isoformat()
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    rows = []
    unresolved_event_times = []
    for index, match in enumerate(matches):
        home_identity = team_identity_evidence(match.get("team1"), strip_fc_suffix=True)
        away_identity = team_identity_evidence(match.get("team2"), strip_fc_suffix=True)
        raw_date_value = match.get("date")
        raw_time_value = match.get("time")
        raw_date = str(raw_date_value) if raw_date_value is not None else None
        raw_time = str(raw_time_value) if raw_time_value is not None else None
        source_id = f"{snapshot['source_file_path']}:{index}"
        venue = dict((venue_metadata or {}).get(source_id, {}))
        venue_name = venue.get("venue_name") or _venue_value(
            match, ("venue_name", "name", "stadium")
        )
        venue_country = venue.get("venue_country") or _venue_value(
            match, ("venue_country", "country")
        )
        venue_timezone = venue.get("venue_timezone") or _venue_value(
            match, ("venue_timezone", "timezone", "timeZone", "tz")
        )
        timezone_name, timezone_basis = resolve_venue_timezone(
            league=league,
            venue_name=venue_name,
            venue_country=venue_country,
            venue_timezone=venue_timezone,
        )
        local_event_time = None
        if raw_date and raw_time:
            parsed = pd.to_datetime(f"{raw_date} {raw_time}", errors="coerce")
            if not pd.isna(parsed):
                candidate = parsed.to_pydatetime()
                if candidate.tzinfo is None or candidate.utcoffset() is None:
                    local_event_time = candidate
        resolution_error = None
        event_time = None
        if local_event_time is None:
            resolution_error = "missing_or_invalid_date_time"
        elif timezone_name is None:
            resolution_error = timezone_basis
        else:
            try:
                event_time = resolve_local_event_time(local_event_time, timezone_name)
            except ValueError as error:
                resolution_error = str(error)
        if event_time is None:
            unresolved_event_times.append({
                "source_id": source_id,
                "source_event_date_raw": raw_date,
                "source_event_time_raw": raw_time,
                "venue_name": venue_name,
                "venue_country": venue_country,
                "venue_timezone": timezone_name,
                "resolution_error": resolution_error,
            })

        score = match.get("score") or {}
        full_time = score.get("ft") or [None, None]
        raw_reference = json.dumps({
            "provider": "openfootball",
            "source_repository": snapshot["source_repository"],
            "source_file_path": snapshot["source_file_path"],
            "source_snapshot_id": snapshot["source_snapshot_id"],
            "source_commit_sha": snapshot["source_commit_sha"],
            "snapshot_sha256": snapshot_sha256,
            "source_record_index": index,
            "source_event_date_raw": raw_date,
            "source_event_time_raw": raw_time,
            "venue_name": venue_name,
            "venue_country": venue_country,
            "venue_timezone": timezone_name,
            "timezone_resolution_basis": timezone_basis if event_time is not None else None,
            "event_time_resolution_error": resolution_error,
            "source_row_evidence": build_source_row_evidence(
                source="openfootball",
                legacy_source_id=source_id,
                snapshot_id=snapshot["source_snapshot_id"],
                snapshot_sha256=snapshot_sha256,
                source_repository=snapshot["source_repository"],
                source_file_path=snapshot["source_file_path"],
                filename=LEAGUE_FILES[league],
                row_locator={"type": "matches_array_index", "value": index},
                raw_row_content=match,
                availability={
                    "status": "snapshot_available",
                    "basis": snapshot["availability_basis"],
                    "available_at": snapshot["source_available_ts"],
                    "snapshot_available_at": snapshot["snapshot_available_at"],
                    "provider_published_at": None,
                    "available_ref": snapshot["source_available_ref"],
                    "interpretation": (
                        "Git commit time establishes this file snapshot's availability, "
                        "not individual match publication"
                    ),
                },
                logical_identity_unavailable_reason=(
                    "OpenFootball rows do not provide a stable provider-issued match identifier"
                ),
                adapter_enrichment={
                    "venue_metadata": {
                        "values": venue or None,
                        "availability_status": "unknown" if venue else "not_supplied",
                        "content_hash": None,
                    },
                    "team_identity": {
                        "home": home_identity,
                        "away": away_identity,
                    },
                },
            ),
        }, sort_keys=True)
        event_time_text = event_time.isoformat() if event_time is not None else None
        rows.append({
            "match_id": f"openfootball-{season}-{index}",
            "league": league,
            "season": fdco_season(season),
            "match_date_utc": event_time_text,
            "source_event_date_raw": raw_date,
            "source_event_time_raw": raw_time,
            "venue_name": venue_name,
            "venue_country": venue_country,
            "venue_timezone": timezone_name,
            "timezone_resolution_basis": timezone_basis if event_time is not None else None,
            "event_time_resolution_error": resolution_error,
            "home_team": canonical_team(match.get("team1")),
            "away_team": canonical_team(match.get("team2")),
            "home_goals": full_time[0],
            "away_goals": full_time[1],
            "source": "openfootball",
            "source_id": source_id,
            "source_event_ts": event_time_text,
            "source_published_ts": None,
            "source_available_ts": snapshot["source_available_ts"],
            "source_available_ref": snapshot["source_available_ref"],
            "source_repository": snapshot["source_repository"],
            "source_file_path": snapshot["source_file_path"],
            "source_commit_sha": snapshot["source_commit_sha"],
            "first_observed_at": None,
            "snapshot_available_at": snapshot["snapshot_available_at"],
            "availability_basis": snapshot["availability_basis"],
            "source_snapshot_id": snapshot["source_snapshot_id"],
            "source_snapshot_sha256": snapshot_sha256,
            "snapshot_row_identity": json.loads(raw_reference)["source_row_evidence"]["identity"]["snapshot_scoped_row_id"],
            "ingest_ts": ingest_ts,
            "original_filename": LEAGUE_FILES[league],
            "parser_version": "fetch_openfootball_v1",
            "raw_reference": raw_reference,
        })

    outdir.mkdir(parents=True, exist_ok=True)
    output = outdir / f"openfootball_{league}_{season.replace('-', '')}.csv"
    pd.DataFrame(rows).to_csv(output, index=False)
    LOG.info("Wrote %s records to %s", len(rows), output)
    unresolved_path = outdir / f"unresolved_event_times_openfootball_{league}_{season.replace('-', '')}.csv"
    pd.DataFrame(
        unresolved_event_times,
        columns=[
            "source_id",
            "source_event_date_raw",
            "source_event_time_raw",
            "venue_name",
            "venue_country",
            "venue_timezone",
            "resolution_error",
        ],
    ).to_csv(unresolved_path, index=False)
    LOG.info(
        "Resolved %d of %d event times; quarantined %d unresolved rows in %s",
        len(rows) - len(unresolved_event_times),
        len(rows),
        len(unresolved_event_times),
        unresolved_path,
    )
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", default="2023-24")
    parser.add_argument("--league", default="premier-league")
    parser.add_argument("--outdir", default="data/processed")
    parser.add_argument("--venue-metadata", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    metadata = load_venue_metadata(args.venue_metadata) if args.venue_metadata else None
    fetch_matches(args.season, args.league, Path(args.outdir), venue_metadata=metadata)


if __name__ == "__main__":
    main()