from collections.abc import Generator
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from src.api.schemas import (
    MatchDetailResponse,
    MatchResponse,
    ModelResponse,
    Page,
    PredictionResponse,
)
from src.db.repositories import MatchRepository, ModelRepository, PredictionRepository
from src.db.session import create_session_factory


def _validate_range(event_from: datetime | None, event_to: datetime | None) -> None:
    for field, value in (("event_from", event_from), ("event_to", event_to)):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise HTTPException(status_code=422, detail=f"{field} must include a timezone offset")
    if event_from is not None and event_to is not None and event_from > event_to:
        raise HTTPException(status_code=422, detail="event_from must be before or equal to event_to")


def create_app(session_factory: sessionmaker[Session] | None = None) -> FastAPI:
    app = FastAPI(
        title="Sports Predictions API",
        version="0.1.0",
        description="Read-only API for persisted sports match and prediction records.",
    )
    app.state.session_factory = session_factory

    def get_session() -> Generator[Session, None, None]:
        factory = app.state.session_factory
        if factory is None:
            try:
                factory = create_session_factory()
                app.state.session_factory = factory
            except RuntimeError as error:
                raise HTTPException(status_code=503, detail="Database is not configured") from error
        with factory() as session:
            yield session

    SessionDependency = Annotated[Session, Depends(get_session)]

    @app.get("/health")
    def health(session: SessionDependency):
        try:
            session.execute(text("SELECT 1"))
        except Exception as error:
            raise HTTPException(status_code=503, detail="Database unavailable") from error
        return {"status": "ok", "version": app.version, "dependencies": {"database": "ok"}}

    @app.get("/matches", response_model=Page[MatchResponse])
    def list_matches(
        session: SessionDependency,
        league: str | None = None,
        season: str | None = None,
        event_from: datetime | None = None,
        event_to: datetime | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        _validate_range(event_from, event_to)
        items = MatchRepository(session).list(
            league=league,
            season=season,
            event_from=event_from,
            event_to=event_to,
            status=status,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "limit": limit, "offset": offset}

    @app.get("/matches/{match_id}", response_model=MatchDetailResponse)
    def get_match(match_id: UUID, session: SessionDependency):
        match = MatchRepository(session).detail(match_id)
        if match is None:
            raise HTTPException(status_code=404, detail="Match not found")
        return match

    @app.get("/predictions", response_model=Page[PredictionResponse])
    def list_predictions(
        session: SessionDependency,
        match_id: UUID | None = None,
        model_id: UUID | None = None,
        feature_version: str | None = None,
        event_from: datetime | None = None,
        event_to: datetime | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        _validate_range(event_from, event_to)
        items = PredictionRepository(session).list(
            match_id=match_id,
            model_id=model_id,
            feature_version=feature_version,
            event_from=event_from,
            event_to=event_to,
            status=status,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "limit": limit, "offset": offset}

    @app.get("/predictions/{prediction_id}", response_model=PredictionResponse)
    def get_prediction(prediction_id: UUID, session: SessionDependency):
        prediction = PredictionRepository(session).get(prediction_id)
        if prediction is None:
            raise HTTPException(status_code=404, detail="Prediction not found")
        return prediction

    @app.get("/models", response_model=Page[ModelResponse])
    def list_models(
        session: SessionDependency,
        model_type: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        items = ModelRepository(session).list(
            model_type=model_type,
            status=status,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "limit": limit, "offset": offset}

    @app.get("/models/{model_id}", response_model=list[ModelResponse])
    def get_model(model_id: UUID, session: SessionDependency, model_version: str | None = None):
        versions = ModelRepository(session).get(model_id, model_version)
        if not versions:
            raise HTTPException(status_code=404, detail="Model not found")
        return versions

    return app


app = create_app()