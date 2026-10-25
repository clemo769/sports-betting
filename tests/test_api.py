from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
import asyncio

import httpx

from src.api import app as api_module
from src.api.app import create_app


class FakeSession:
    def execute(self, statement):
        return 1

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def factory():
    return FakeSession()


def request(app, path):
    async def send_request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(send_request())


def match_record(match_id):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return SimpleNamespace(
        match_id=match_id,
        sport="football",
        league="premier-league",
        season="2526",
        home_team="Arsenal",
        away_team="Chelsea",
        event_time=now,
        status="scheduled",
        created_at=now,
        updated_at=now,
        source_records=[],
        result_observations=[],
    )


def test_health_reports_database_ready():
    response = request(create_app(factory), "/health")

    assert response.status_code == 200
    assert response.json()["dependencies"]["database"] == "ok"


def test_match_list_applies_filters_and_returns_page(monkeypatch):
    requested = {}

    class FakeMatchRepository:
        def __init__(self, session):
            pass

        def list(self, **kwargs):
            requested.update(kwargs)
            return [match_record(uuid4())]

    monkeypatch.setattr(api_module, "MatchRepository", FakeMatchRepository)
    response = request(create_app(factory),
        "/matches?league=premier-league&season=2526&limit=25&offset=5"
    )

    assert response.status_code == 200
    assert response.json()["limit"] == 25
    assert len(response.json()["items"]) == 1
    assert requested["league"] == "premier-league"
    assert requested["season"] == "2526"
    assert requested["limit"] == 25
    assert requested["offset"] == 5


def test_match_detail_returns_404_when_absent(monkeypatch):
    class FakeMatchRepository:
        def __init__(self, session):
            pass

        def detail(self, match_id):
            return None

    monkeypatch.setattr(api_module, "MatchRepository", FakeMatchRepository)
    response = request(create_app(factory), f"/matches/{uuid4()}")

    assert response.status_code == 404


def test_naive_event_range_is_rejected():
    response = request(create_app(factory),
        "/matches?event_from=2026-01-01T00:00:00"
    )

    assert response.status_code == 422
    assert "timezone offset" in response.json()["detail"]


def test_prediction_list_and_model_list_are_read_only_contracts(monkeypatch):
    class EmptyPredictionRepository:
        def __init__(self, session):
            pass

        def list(self, **kwargs):
            return []

    class EmptyModelRepository:
        def __init__(self, session):
            pass

        def list(self, **kwargs):
            return []

    monkeypatch.setattr(api_module, "PredictionRepository", EmptyPredictionRepository)
    monkeypatch.setattr(api_module, "ModelRepository", EmptyModelRepository)
    app = create_app(factory)
    predictions = request(app, "/predictions?status=published")
    models = request(app, "/models?status=passed")

    assert predictions.status_code == 200
    assert predictions.json() == {"items": [], "limit": 100, "offset": 0}
    assert models.status_code == 200
    assert models.json() == {"items": [], "limit": 100, "offset": 0}


def test_model_detail_returns_404_when_absent(monkeypatch):
    class EmptyModelRepository:
        def __init__(self, session):
            pass

        def get(self, model_id, model_version=None):
            return []

    monkeypatch.setattr(api_module, "ModelRepository", EmptyModelRepository)
    response = request(create_app(factory), f"/models/{uuid4()}")

    assert response.status_code == 404