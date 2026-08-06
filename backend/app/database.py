import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import get_settings

logger = logging.getLogger("rootcause.database")

settings = get_settings()


def _resolve_database_url() -> str:
    """Uses Vault dynamic DB credentials when Vault's database secrets
    engine is configured; otherwise the static DATABASE_URL. See
    app/secrets.py and docs/vault-setup.md."""
    try:
        from .secrets import get_vault_client
        return get_vault_client().get_database_url()
    except Exception:  # noqa: BLE001 -- never let secrets resolution block DB import
        logger.warning("Falling back to static DATABASE_URL (Vault secrets resolution failed)")
        return settings.database_url


# pool_pre_ping keeps this resilient to the demo db not being up yet; the
# engine is lazily connected to so importing this module never fails even
# without a live postgres (needed for py_compile / import sanity checks and
# for exercising the agent pipeline directly in run_demo.py without a DB).
engine = create_engine(_resolve_database_url(), pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    DEMO_MODE / SQLite fallback ONLY. In a real deployment (Postgres,
    DEMO_MODE=false), schema management is Alembic's job -- run
    `alembic upgrade head` (see backend/alembic/) as a migration step before
    starting the API, and do NOT rely on this function to create tables.

    Why the split: Alembic autogenerate against SQLite is unreliable (SQLite
    lacks native ALTER TABLE support for most operations, so Alembic has to
    do table-rebuild workarounds that don't always autogenerate cleanly),
    and the whole point of DEMO_MODE/run_demo.py is zero-setup -- forcing an
    `alembic upgrade head` step there would defeat that. So: SQLite/DEMO_MODE
    keeps the old create_all() behavior; Postgres/production is expected to
    use Alembic exclusively.
    """
    if not (settings.demo_mode or str(engine.url).startswith("sqlite")):
        logger.info("init_db(): DEMO_MODE is false and this is not SQLite -- skipping create_all(); "
                    "run 'alembic upgrade head' to manage schema instead.")
        return
    from . import models  # noqa: F401 ensure models are registered
    Base.metadata.create_all(bind=engine)
