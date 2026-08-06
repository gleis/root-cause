"""
Shared pytest fixtures. Forces DEMO_MODE + a throwaway SQLite DB before any
app module is imported, mirroring backend/scripts/run_demo.py, so the whole
suite runs with zero external infrastructure (no Postgres/Redis/Vault/IdP).
"""
import os
import sys
import tempfile

os.environ["DEMO_MODE"] = "true"
_TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "rootcause_test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
os.environ.setdefault("AUTH_ALLOW_ANONYMOUS_IN_DEMO", "true")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: E402


@pytest.fixture()
def db_session():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_session):
    """A TestClient wired to the same DB session/engine as db_session, via
    FastAPI's dependency override for get_db."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
