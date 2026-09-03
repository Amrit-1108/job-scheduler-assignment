import os

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://postgres@localhost:5432/scheduler_test",
)


def _ensure_test_database() -> None:
    """CREATE DATABASE if it isn't there yet, so `pytest` just works."""
    admin_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    dsn = admin_url.replace("postgresql+psycopg2://", "postgresql://")
    name = TEST_DATABASE_URL.rsplit("/", 1)[1]
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()


_ensure_test_database()

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("TASK_MIN_SECONDS", "0")
os.environ.setdefault("TASK_MAX_SECONDS", "0")
os.environ.setdefault("LEASE_SECONDS", "30")
os.environ.setdefault("RETRY_BACKOFF_SECONDS", "5")
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough-for-hs256-abcdef")

from fastapi.testclient import TestClient   

from app.api.deps import get_session   
from app.core.db import SessionLocal, engine, init_db   
from app.domain.models import Base  
from app.main import app   
from app.services.job_service import JobService   


@pytest.fixture(scope="session", autouse=True)
def _database():
    Base.metadata.drop_all(bind=engine)  
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables():
    """Start every test from an empty database."""
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


@pytest.fixture
def session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def service(session) -> JobService:
    return JobService(session)


@pytest.fixture
def anon_client(session):
    """No token attached - for asserting that endpoints are actually guarded."""
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _register_and_login(test_client, username: str, password: str = "supersecret123") -> str:
    test_client.post("/auth/register", json={"username": username, "password": password})
    token = test_client.post(
        "/auth/token", json={"username": username, "password": password}
    ).json()["access_token"]
    return token


@pytest.fixture
def client(anon_client):
    """Logged in as `tester`. Most tests care about jobs, not about auth."""
    token = _register_and_login(anon_client, "tester")
    anon_client.headers.update({"Authorization": f"Bearer {token}"})
    return anon_client


@pytest.fixture
def other_client(session):
    """A second, separate account - used to prove jobs don't leak across users."""
    other = TestClient(app)
    app.dependency_overrides[get_session] = lambda: session
    token = _register_and_login(other, "someone-else")
    other.headers.update({"Authorization": f"Bearer {token}"})
    return other
