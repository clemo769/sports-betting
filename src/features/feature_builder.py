"""Build time-safe pre-match features from canonical match records."""
import argparse
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
from uuid import UUID

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session, sessionmaker

from src.db.as_of import (
    AsOfSelection,
    AsOfSelectionRepository,
    AsOfSelectionService,
)
from src.db.models import Match
from src.db.repositories import MatchRepository
from src.db.session import create_session_factory
from src.features.temporal_contract import (
    validate_prediction_cutoff,
    validate_prediction_mode,
    validate_timezone_aware_utc,
)
from src.temporal import PredictionMode, PREDICTION_MODE_VALUES


def _event_groups(matches_df: pd.DataFrame):
    """Yield valid event-time groups in deterministic order."""
    times = pd.to_datetime(matches_df["match_date_utc"], errors="coerce")
    valid = matches_df.index[times.notna()]
    keys = matches_df.loc[valid].copy()
    keys["_event_time"] = times.loc[valid]
    keys["_tie_key"] = keys.apply(
        lambda row: "|".join(str(row.get(column, "")) for column in ("match_id", "source", "source_id")),
        axis=1,
    )
    keys = keys.sort_values(["_event_time", "_tie_key"], kind="mergesort")
    for _, group in keys.groupby("_event_time", sort=False):
        yield list(group.index)


def _team_match_points(row: pd.Series):
    home_goals = row.get("home_goals")
    away_goals = row.get("away_goals")
    if pd.isna(home_goals) or pd.isna(away_goals):
        return None, None
    if home_goals > away_goals:
        return 3, 0
    if home_goals < away_goals:
        return 0, 3
    return 1, 1


def compute_elo_series(matches_df: pd.DataFrame, k: float = 20.0):
    """Return pre-match ``(home, away)`` Elo ratings aligned to input rows.

    Results from all matches at an event timestamp are applied after features
    for that timestamp are recorded, preventing same-time cross-match leakage.
    """
    elo = {}
    ratings = {}
    for indices in _event_groups(matches_df):
        for index in indices:
            row = matches_df.loc[index]
            home = row["home_team"]
            away = row["away_team"]
            elo.setdefault(home, 1500.0)
            elo.setdefault(away, 1500.0)
            ratings[index] = (elo[home], elo[away])

        for index in indices:
            row = matches_df.loc[index]
            home = row["home_team"]
            away = row["away_team"]
            home_goals, away_goals = row.get("home_goals"), row.get("away_goals")
            if pd.isna(home_goals) or pd.isna(away_goals):
                continue
            expected_home = 1.0 / (1.0 + 10 ** ((elo[away] - elo[home]) / 400.0))
            score_home = 1.0 if home_goals > away_goals else 0.5 if home_goals == away_goals else 0.0
            elo[home] += k * (score_home - expected_home)
            elo[away] += k * ((1.0 - score_home) - (1.0 - expected_home))

    return [ratings.get(index, (1500.0, 1500.0)) for index in matches_df.index]


def compute_form_series(matches_df: pd.DataFrame, lookback: int = 3):
    """Return pre-match points form keyed by ``(row index, team)``."""
    histories = {}
    form = {}
    for indices in _event_groups(matches_df):
        for index in indices:
            row = matches_df.loc[index]
            for team in (row["home_team"], row["away_team"]):
                previous = histories.get(team, [])
                form[(index, team)] = np.mean(previous[-lookback:]) if len(previous) >= lookback else np.nan

        for index in indices:
            row = matches_df.loc[index]
            home_points, away_points = _team_match_points(row)
            if home_points is not None:
                histories.setdefault(row["home_team"], []).append(home_points)
                histories.setdefault(row["away_team"], []).append(away_points)
    return form


def _sort_for_output(matches_df: pd.DataFrame) -> pd.DataFrame:
    output = matches_df.copy()
    output["_event_time"] = pd.to_datetime(output["match_date_utc"], errors="coerce")
    output["_tie_key"] = output.apply(
        lambda row: "|".join(str(row.get(column, "")) for column in ("match_id", "source", "source_id")),
        axis=1,
    )
    return output.sort_values(["_event_time", "_tie_key"], na_position="last", kind="mergesort").drop(
        columns=["_event_time", "_tie_key"]
    ).reset_index(drop=True)


def _resolve_prediction_cutoffs(df: pd.DataFrame, prediction_cutoff, prediction_cutoffs):
    if prediction_cutoff is not None and prediction_cutoffs is not None:
        raise ValueError("provide either prediction_cutoff or prediction_cutoffs, not both")
    if prediction_cutoffs is None:
        if prediction_cutoff is None:
            return None
        cutoff = validate_prediction_cutoff(prediction_cutoff)
        values = pd.Series(cutoff, index=df.index, dtype="object")
    else:
        if "match_id" not in df.columns:
            raise ValueError("per-match prediction cutoffs require a match_id column")
        if isinstance(prediction_cutoffs, pd.Series):
            values_by_id = prediction_cutoffs.to_dict()
        elif isinstance(prediction_cutoffs, dict):
            values_by_id = prediction_cutoffs
        else:
            raise TypeError("prediction_cutoffs must be a mapping or pandas Series keyed by match_id")
        match_ids = df["match_id"].tolist()
        missing = [match_id for match_id in match_ids if match_id not in values_by_id]
        if missing:
            raise ValueError(f"prediction_cutoffs missing match_id values: {missing[:3]}")
        values = pd.Series(
            [validate_prediction_cutoff(values_by_id[match_id]) for match_id in match_ids],
            index=df.index,
            dtype="object",
        )

    event_times = pd.to_datetime(df["match_date_utc"], errors="coerce")
    for index, event_time in event_times.items():
        if pd.isna(event_time):
            continue
        cutoff = values.loc[index]
        if (event_time.tzinfo is None) != (cutoff.tzinfo is None):
            raise ValueError("match event times and prediction cutoffs must both be timezone-naive or timezone-aware")
        if event_time > cutoff:
            raise ValueError(
                f"prediction cutoff cannot be earlier than match event time; cutoff is earlier than a match event at row {index}"
            )
    return values


def build_as_of_features(
    target_matches: Iterable[Match],
    *,
    as_of_selector: AsOfSelectionService,
    prediction_cutoffs: Mapping[str | UUID, datetime | str],
    prediction_mode: str | PredictionMode,
    feature_version: str,
    schema_version: str,
    feature_generation_version: str,
    out_path: Path | None = None,
) -> pd.DataFrame:
    """Build the existing Elo/form features from each target's as-of selection."""
    mode = validate_prediction_mode(prediction_mode)
    for field_name, value in (
        ("feature_version", feature_version),
        ("schema_version", schema_version),
        ("feature_generation_version", feature_generation_version),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")

    targets = list(target_matches)
    target_ids = [target.match_id for target in targets]
    if len(target_ids) != len(set(target_ids)):
        raise ValueError("target matches contain duplicate match_id values")

    rows = []
    blocking_issue_codes = {
        "invalid_availability_evidence",
        "disputed_result_as_of_cutoff",
        "resolution_after_cutoff",
        "superseded_without_successor",
        "conflicting_active_results",
        "ambiguous_source_revision",
        "invalid_source_revision_evidence",
        "target_match_metadata_unresolved",
        "target_match_team_identity_unresolved",
    }
    for target in targets:
        target_id = target.match_id
        cutoff_value = prediction_cutoffs.get(target_id, prediction_cutoffs.get(str(target_id)))
        if cutoff_value is None:
            raise ValueError(f"prediction_cutoffs missing target match_id {target_id}")
        cutoff = validate_prediction_cutoff(cutoff_value)
        cutoff_datetime = cutoff.to_pydatetime()
        selection = as_of_selector.select(
            target,
            prediction_cutoff=cutoff_datetime,
            prediction_mode=mode,
        )
        if selection.unresolved_issues:
            blocking = [
                issue for issue in selection.unresolved_issues
                if issue.code in blocking_issue_codes
            ]
            if blocking:
                codes = ", ".join(sorted({issue.code for issue in blocking}))
                raise ValueError(f"as-of selection for match {target_id} has unresolved issues: {codes}")

        results_by_match: dict[UUID, dict[str, Any]] = {}
        selected_observation_ids_by_match: dict[UUID, list[UUID]] = defaultdict(list)
        source_record_ids_by_match: dict[UUID, set[UUID]] = defaultdict(set)
        for observation in selection.selected_result_observations:
            result = observation.result_data
            try:
                home_goals = int(result["home_goals"])
                away_goals = int(result["away_goals"])
            except (KeyError, TypeError, ValueError, OverflowError):
                raise ValueError(
                    f"result observation {observation.result_observation_id} has invalid goal data"
                ) from None
            if home_goals < 0 or away_goals < 0:
                raise ValueError(
                    f"result observation {observation.result_observation_id} has negative goals"
                )
            candidate = {
                "match_id": str(observation.match_id),
                "match_date_utc": observation.event_time.isoformat() if observation.event_time else None,
                "home_team": observation.home_team,
                "away_team": observation.away_team,
                "home_goals": home_goals,
                "away_goals": away_goals,
            }
            previous = results_by_match.get(observation.match_id)
            if previous is not None and previous != candidate:
                raise ValueError(
                    f"selected result observations disagree for historical match {observation.match_id}"
                )
            results_by_match[observation.match_id] = candidate
            selected_observation_ids_by_match[observation.match_id].append(
                observation.result_observation_id
            )
            if observation.source_record_id is not None:
                source_record_ids_by_match[observation.match_id].add(observation.source_record_id)

        historical_rows = sorted(
            results_by_match.values(),
            key=lambda row: (row["match_date_utc"], row["match_id"]),
        )
        if any(
            row["match_date_utc"] is None or row["home_team"] is None or row["away_team"] is None
            for row in historical_rows
        ):
            raise ValueError(f"as-of selection for match {target_id} returned incomplete prior match metadata")
        target_metadata = selection.target_metadata
        if target_metadata.event_time is None or target_metadata.home_team is None or target_metadata.away_team is None:
            raise ValueError(f"as-of selection for match {target_id} has incomplete target match metadata")
        target_row = {
            "match_id": str(target_id),
            "match_date_utc": target_metadata.event_time.isoformat(),
            "home_team": target_metadata.home_team,
            "away_team": target_metadata.away_team,
            "home_goals": np.nan,
            "away_goals": np.nan,
        }
        combined = pd.DataFrame([*historical_rows, target_row]).reset_index(drop=True)
        target_index = len(combined) - 1
        elo_values = compute_elo_series(combined)
        form_values = compute_form_series(combined)
        target_elo = elo_values[target_index]
        target_home_form = form_values.get((target_index, target_metadata.home_team), np.nan)
        target_away_form = form_values.get((target_index, target_metadata.away_team), np.nan)

        contributing_source_ids = sorted(
            {
                source_record_id
                for source_ids in source_record_ids_by_match.values()
                for source_record_id in source_ids
            },
            key=str,
        )
        selected_sources = {
            source.source_record_id: source
            for source in selection.selected_source_records
            if source.source_record_id in contributing_source_ids
        }
        missing_source_evidence = set(contributing_source_ids) - set(selected_sources)
        if missing_source_evidence:
            missing_ids = ", ".join(str(value) for value in sorted(missing_source_evidence, key=str))
            raise ValueError(f"selected result sources lack source-record evidence: {missing_ids}")
        if mode is PredictionMode.STRICT_PRE_MATCH:
            missing_snapshot_ids = [
                source_id
                for source_id in contributing_source_ids
                if not selected_sources[source_id].source_snapshot_id
            ]
            if missing_snapshot_ids:
                missing_ids = ", ".join(str(value) for value in sorted(missing_snapshot_ids, key=str))
                raise ValueError(
                    "STRICT_PRE_MATCH requires immutable source snapshot identity for every "
                    f"contributing source record: {missing_ids}"
                )
        evidence_rows = [
            {
                "source_record_id": str(source_id),
                "source": selected_sources[source_id].source,
                "source_id": selected_sources[source_id].source_id,
                "source_revision": selected_sources[source_id].source_revision,
                "availability_basis": selected_sources[source_id].availability_basis,
                "availability_status": selected_sources[source_id].availability_status,
                "source_available_at": (
                    selected_sources[source_id].source_available_at.isoformat()
                    if selected_sources[source_id].source_available_at is not None else None
                ),
                "source_published_at": (
                    selected_sources[source_id].source_published_at.isoformat()
                    if selected_sources[source_id].source_published_at is not None else None
                ),
                "first_observed_at": (
                    selected_sources[source_id].first_observed_at.isoformat()
                    if selected_sources[source_id].first_observed_at is not None else None
                ),
                "snapshot_available_at": (
                    selected_sources[source_id].snapshot_available_at.isoformat()
                    if selected_sources[source_id].snapshot_available_at is not None else None
                ),
                "source_available_ref": selected_sources[source_id].source_available_ref,
                "source_snapshot_id": selected_sources[source_id].source_snapshot_id,
                "source_snapshot_sha256": selected_sources[source_id].source_snapshot_sha256,
            }
            for source_id in contributing_source_ids
            if source_id in selected_sources
        ]
        selected_observation_ids = sorted(
            {
                observation_id
                for ids in selected_observation_ids_by_match.values()
                for observation_id in ids
            },
            key=str,
        )
        unknown_availability = any(
            row["availability_status"] == "unknown" for row in evidence_rows
        ) or any(
            observation.availability_status == "unknown"
            for observation in selection.selected_result_observations
        ) or selection.target_metadata.availability_status != "verified" or any(
            metadata.availability_status != "verified"
            for metadata in selection.prior_match_metadata
        )
        snapshot_ids = sorted({
            row["source_snapshot_id"]
            for row in evidence_rows
            if row["source_snapshot_id"]
        })
        source_record_ids = [str(value) for value in contributing_source_ids]
        observation_ids = [str(value) for value in selected_observation_ids]
        selection_identity = {
            "match_id": str(target_id),
            "prediction_mode": mode.value,
            "prediction_cutoff": cutoff_datetime.isoformat(),
            "target_match_metadata": target_metadata.as_dict(),
            "prior_match_metadata": [metadata.as_dict() for metadata in selection.prior_match_metadata],
            "source_record_ids": source_record_ids,
            "result_observation_ids": observation_ids,
            "source_snapshot_ids": snapshot_ids,
        }
        selection_hash = hashlib.sha256(
            json.dumps(selection_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        source_data_reference = {
            "snapshot_id": f"asof-selection-sha256:{selection_hash}",
            "source_record_ids": source_record_ids,
            "snapshot_ids": snapshot_ids,
            "result_observation_ids": observation_ids,
            "target_match_metadata": target_metadata.as_dict(),
            "prior_match_metadata": [metadata.as_dict() for metadata in selection.prior_match_metadata],
        }
        rows.append({
            "match_id": str(target_id),
            "match_date_utc": target_row["match_date_utc"],
            "scheduled_event_time": target_row["match_date_utc"],
            "prediction_cutoff": cutoff_datetime.isoformat(),
            "prediction_mode": mode.value,
            "home_team": target_metadata.home_team,
            "away_team": target_metadata.away_team,
            "home_elo": target_elo[0],
            "away_elo": target_elo[1],
            "home_form_last3": None if pd.isna(target_home_form) else float(target_home_form),
            "away_form_last3": None if pd.isna(target_away_form) else float(target_away_form),
            "feature_version": feature_version,
            "schema_version": schema_version,
            "feature_generation_version": feature_generation_version,
            "source_record_ids_json": json.dumps(
                [str(value) for value in contributing_source_ids], separators=(",", ":")
            ),
            "result_observation_ids_json": json.dumps(
                [str(value) for value in selected_observation_ids], separators=(",", ":")
            ),
            "source_availability_evidence_json": json.dumps(
                evidence_rows, sort_keys=True, separators=(",", ":")
            ),
            "source_data_reference_json": json.dumps(
                source_data_reference, sort_keys=True, separators=(",", ":")
            ),
            "target_match_metadata_json": json.dumps(
                target_metadata.as_dict(), sort_keys=True, separators=(",", ":")
            ),
            "prior_match_metadata_json": json.dumps(
                [metadata.as_dict() for metadata in selection.prior_match_metadata],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "unknown_availability": unknown_availability,
            "excluded_source_records_json": json.dumps(
                [
                    {
                        "source_record_id": str(item.record_id),
                        "match_id": str(item.match_id) if item.match_id else None,
                        "reason": item.reason,
                    }
                    for item in selection.excluded_source_records
                ],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "excluded_result_observations_json": json.dumps(
                [
                    {
                        "result_observation_id": str(item.record_id),
                        "match_id": str(item.match_id) if item.match_id else None,
                        "reason": item.reason,
                    }
                    for item in selection.excluded_result_observations
                ],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "unresolved_temporal_issues_json": json.dumps(
                [
                    {
                        "code": issue.code,
                        "message": issue.message,
                        "match_id": str(issue.match_id) if issue.match_id else None,
                        "observation_id": str(issue.observation_id) if issue.observation_id else None,
                        "source_record_id": str(issue.source_record_id) if issue.source_record_id else None,
                    }
                    for issue in selection.unresolved_issues
                ],
                sort_keys=True,
                separators=(",", ":"),
            ),
        })

    result = pd.DataFrame(rows)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_parquet(out_path, index=False)
        logging.info("Wrote %d as-of feature rows to %s", len(result), out_path)
    return result


def build_as_of_features_from_database(
    out_path: Path,
    *,
    prediction_cutoffs: Mapping[str | UUID, datetime | str],
    prediction_mode: str | PredictionMode,
    feature_version: str,
    schema_version: str,
    feature_generation_version: str,
    database_url: str | None = None,
    league: str | None = None,
    season: str | None = None,
) -> pd.DataFrame:
    """Load persisted target matches and use the database-backed as-of selector."""
    factory = create_session_factory(database_url)
    try:
        with factory() as session:
            match_repository = MatchRepository(session)
            targets = []
            offset = 0
            while True:
                batch = match_repository.list(
                    league=league,
                    season=season,
                    limit=500,
                    offset=offset,
                )
                targets.extend(batch)
                if len(batch) < 500:
                    break
                offset += len(batch)
            if not targets:
                raise ValueError("no target matches found in the database")
            selector = AsOfSelectionService(AsOfSelectionRepository(session))
            return build_as_of_features(
                targets,
                as_of_selector=selector,
                prediction_cutoffs=prediction_cutoffs,
                prediction_mode=prediction_mode,
                feature_version=feature_version,
                schema_version=schema_version,
                feature_generation_version=feature_generation_version,
                out_path=out_path,
            )
    finally:
        factory.kw["bind"].dispose()


def load_prediction_cutoffs(path: Path) -> dict[str, datetime]:
    """Load explicit timezone-aware prediction cutoffs keyed by match UUID."""
    with path.open(encoding="utf-8") as cutoff_file:
        values = json.load(cutoff_file)
    if not isinstance(values, dict):
        raise ValueError("prediction cutoff file must be a JSON object keyed by match_id")
    cutoffs = {}
    for match_id, cutoff_value in values.items():
        try:
            UUID(match_id)
        except (TypeError, ValueError):
            raise ValueError(f"prediction cutoff key {match_id!r} must be a UUID") from None
        cutoff = validate_prediction_cutoff(cutoff_value)
        cutoffs[match_id] = cutoff.to_pydatetime()
    return cutoffs


def build_features(matches_path: Path, out_path: Path, prediction_cutoff=None, prediction_cutoffs=None):
    """Build pre-match features with explicit global or per-match cutoffs.

    When omitted, each match uses its own event time as the prediction point.
    A global cutoff applies to every row. A per-match mapping keyed by
    ``match_id`` applies each row's cutoff independently. In all modes,
    historical results still require strictly earlier event times.
    """
    df = pd.read_csv(matches_path, parse_dates=["match_date_utc"])
    resolved_cutoffs = _resolve_prediction_cutoffs(df, prediction_cutoff, prediction_cutoffs)
    if resolved_cutoffs is not None:
        df["prediction_cutoff"] = resolved_cutoffs
    elos = compute_elo_series(df)
    forms = compute_form_series(df)
    df["home_elo"] = [value[0] for value in elos]
    df["away_elo"] = [value[1] for value in elos]
    df["home_form_last3"] = [forms.get((index, row["home_team"]), np.nan) for index, row in df.iterrows()]
    df["away_form_last3"] = [forms.get((index, row["away_team"]), np.nan) for index, row in df.iterrows()]

    df = _sort_for_output(df)
    feature_cols = [
        "match_id", "match_date_utc", "home_team", "away_team",
        "home_elo", "away_elo", "home_form_last3", "away_form_last3",
    ]
    if resolved_cutoffs is not None:
        feature_cols.append("prediction_cutoff")
    feats = df[feature_cols].copy()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    feats.to_parquet(out_path, index=False)
    logging.info("Wrote features to %s", out_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--matches", default="data/processed/matches_canonical.csv")
    parser.add_argument("--out", default="data/processed/features_batch.parquet")
    parser.add_argument("--prediction-cutoff", default=None)
    parser.add_argument("--prediction-mode", choices=PREDICTION_MODE_VALUES)
    parser.add_argument("--prediction-cutoffs", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--league", default=None)
    parser.add_argument("--season", default=None)
    parser.add_argument("--feature-version", default=None)
    parser.add_argument("--schema-version", default=None)
    parser.add_argument("--feature-generation-version", default=None)
    args = parser.parse_args()
    if args.prediction_mode is None:
        if any((args.prediction_cutoffs, args.database_url, args.feature_version,
                args.schema_version, args.feature_generation_version)):
            parser.error("as-of arguments require --prediction-mode")
        build_features(Path(args.matches), Path(args.out), prediction_cutoff=args.prediction_cutoff)
    else:
        if args.prediction_cutoff is not None:
            parser.error("--prediction-cutoff cannot be combined with --prediction-mode")
        required = {
            "--prediction-cutoffs": args.prediction_cutoffs,
            "--feature-version": args.feature_version,
            "--schema-version": args.schema_version,
            "--feature-generation-version": args.feature_generation_version,
        }
        missing = [flag for flag, value in required.items() if not value]
        if missing:
            parser.error(f"as-of mode requires {', '.join(missing)}")
        build_as_of_features_from_database(
            Path(args.out),
            prediction_cutoffs=load_prediction_cutoffs(args.prediction_cutoffs),
            prediction_mode=args.prediction_mode,
            feature_version=args.feature_version,
            schema_version=args.schema_version,
            feature_generation_version=args.feature_generation_version,
            database_url=args.database_url,
            league=args.league,
            season=args.season,
        )
