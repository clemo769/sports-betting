from datetime import datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict


PageItem = TypeVar("PageItem")


class Page(BaseModel, Generic[PageItem]):
    items: list[PageItem]
    limit: int
    offset: int


class MatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    match_id: UUID
    sport: str
    league: str
    season: str
    home_team: str
    away_team: str
    event_time: datetime
    status: str
    created_at: datetime
    updated_at: datetime


class SourceRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_record_id: UUID
    source: str
    source_id: str
    source_revision: int
    source_event_ts: datetime | None
    source_available_ts: datetime | None
    source_available_ref: str | None
    ingest_ts: datetime
    original_filename: str | None
    parser_version: str | None


class ResultObservationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    result_observation_id: UUID
    source_record_id: UUID | None
    result_data: dict[str, Any]
    observed_at: datetime
    observation_status: str
    supersedes_observation_id: UUID | None
    resolution_reason: str | None
    resolved_at: datetime | None
    resolved_by: str | None


class MatchDetailResponse(MatchResponse):
    source_records: list[SourceRecordResponse]
    result_observations: list[ResultObservationResponse]


class PredictionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    prediction_id: UUID
    match_id: UUID
    feature_set_id: UUID
    model_id: UUID
    model_version: str
    feature_version: str
    prediction_cutoff: datetime
    predicted_at: datetime
    prediction: str
    probabilities: dict[str, Any]
    confidence: Decimal | None
    status: str


class ModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    model_id: UUID
    model_version: str
    model_type: str
    feature_version: str
    training_run_id: UUID
    training_data_reference: dict[str, Any]
    artifact_reference: str
    configuration_reference: dict[str, Any] | None
    created_at: datetime
    evaluation_status: str