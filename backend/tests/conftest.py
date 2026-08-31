from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import DEFAULT_LEVELS, app
from app.models import BreakConfig, BreakRule, Level, SlaConfig, SolverConfig


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    for name, rank, counts in DEFAULT_LEVELS:
        session.add(Level(name=name, rank=rank, counts_for_rating=counts))
    session.add(
        SlaConfig(target_seconds=300, adherence_goal=0.9, effective_from=date(2000, 1, 1))
    )
    session.add(SolverConfig())
    session.add(BreakConfig())
    session.add(BreakRule(min_shift_minutes=210, rest_breaks=1, meal_breaks=0))
    session.add(BreakRule(min_shift_minutes=361, rest_breaks=2, meal_breaks=1))
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def manager_headers(client):
    r = client.post(
        "/api/auth/register", json={"email": "boss@yeems.com", "password": "secret123"}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}
