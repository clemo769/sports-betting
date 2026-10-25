from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    FeatureSet,
    Match,
    ModelVersion,
    Prediction,
    ResultObservation,
    SourceRecord,
)


class MatchRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, match_id: UUID) -> Match | None:
        return self.session.get(Match, match_id)

    def list(
        self,
        *,
        league: str | None = None,
        season: str | None = None,
        event_from=None,
        event_to=None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Match]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        statement = select(Match).order_by(Match.event_time, Match.match_id)
        if league is not None:
            statement = statement.where(Match.league == league)
        if season is not None:
            statement = statement.where(Match.season == season)
        if event_from is not None:
            statement = statement.where(Match.event_time >= event_from)
        if event_to is not None:
            statement = statement.where(Match.event_time <= event_to)
        if status is not None:
            statement = statement.where(Match.status == status)
        return list(self.session.scalars(statement.limit(limit).offset(offset)))

    def detail(self, match_id: UUID) -> Match | None:
        match = self.session.get(Match, match_id)
        if match is None:
            return None
        match.source_records = list(
            self.session.scalars(
                select(SourceRecord)
                .where(SourceRecord.match_id == match_id)
                .order_by(SourceRecord.source, SourceRecord.source_id)
            )
        )
        match.result_observations = list(
            self.session.scalars(
                select(ResultObservation)
                .where(ResultObservation.match_id == match_id)
                .order_by(ResultObservation.observed_at.desc())
            )
        )
        return match


class SourceRecordRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, source_record_id: UUID) -> SourceRecord | None:
        return self.session.get(SourceRecord, source_record_id)

    def add(self, record: SourceRecord) -> SourceRecord:
        self.session.add(record)
        self.session.flush()
        return record

    def find_identity(
        self, source: str, source_id: str, source_revision: int = 1
    ) -> SourceRecord | None:
        statement = select(SourceRecord).where(
            SourceRecord.source == source,
            SourceRecord.source_id == source_id,
            SourceRecord.source_revision == source_revision,
        )
        return self.session.scalar(statement)


class PredictionRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, prediction_id: UUID) -> Prediction | None:
        return self.session.get(Prediction, prediction_id)

    def list(
        self,
        *,
        match_id: UUID | None = None,
        model_id: UUID | None = None,
        feature_version: str | None = None,
        event_from=None,
        event_to=None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Prediction]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        statement = select(Prediction).order_by(Prediction.predicted_at.desc(), Prediction.prediction_id)
        if match_id is not None:
            statement = statement.where(Prediction.match_id == match_id)
        if model_id is not None:
            statement = statement.where(Prediction.model_id == model_id)
        if feature_version is not None:
            statement = statement.where(Prediction.feature_version == feature_version)
        if event_from is not None or event_to is not None:
            statement = statement.join(Match, Prediction.match_id == Match.match_id)
            if event_from is not None:
                statement = statement.where(Match.event_time >= event_from)
            if event_to is not None:
                statement = statement.where(Match.event_time <= event_to)
        if status is not None:
            statement = statement.where(Prediction.status == status)
        return list(self.session.scalars(statement.limit(limit).offset(offset)))


class ModelRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, model_id: UUID, model_version: str | None = None) -> list[ModelVersion]:
        statement = select(ModelVersion).where(ModelVersion.model_id == model_id)
        if model_version is not None:
            statement = statement.where(ModelVersion.model_version == model_version)
        return list(self.session.scalars(statement.order_by(ModelVersion.created_at.desc())))

    def list(
        self,
        *,
        model_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ModelVersion]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        statement = select(ModelVersion).order_by(ModelVersion.created_at.desc(), ModelVersion.model_id)
        if model_type is not None:
            statement = statement.where(ModelVersion.model_type == model_type)
        if status is not None:
            statement = statement.where(ModelVersion.evaluation_status == status)
        return list(self.session.scalars(statement.limit(limit).offset(offset)))