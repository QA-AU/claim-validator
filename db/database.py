"""Database initialization and session management."""

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

logger = logging.getLogger(__name__)


def get_database_url(env: str = "local") -> str:
    """Get database URL based on environment.

    `CLAIMVAL_DB_URL` overrides every case. The local path below is resolved
    relative to this file rather than the working directory, so without an
    override every caller on the machine — including the test suite — shares one
    database file and writes into it. Tests must point this somewhere temporary.

    Env var and default filename are both distinct from the source repo this
    file was copied from (`ONTOLOGY_DB_URL` / `ontology_api_testing.db`), so
    the two repos never collide if they're ever run from the same machine.
    """
    override = os.getenv("CLAIMVAL_DB_URL", "").strip()
    if override:
        return override

    if env == "production":
        return os.getenv(
            "DATABASE_URL",
            "postgresql://user:password@localhost/claimval",
        )
    elif env == "docker":
        return os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@db:5432/claimval",
        )
    else:
        # Local SQLite
        db_dir = Path(__file__).parent.parent / ".data"
        db_dir.mkdir(exist_ok=True)
        return f"sqlite:///{db_dir}/claimval.db"


def add_missing_columns(engine) -> list[str]:
    """Add nullable columns the models declare and an existing table lacks.

    `create_all` creates missing *tables* and never alters one that exists. So a
    column added to a model after a database file exists is invisible to that
    file, and the next query naming it fails — for `ProcessPrompt` that failure
    is caught and reported as "registry unavailable", which would silently turn
    off database-held prompts for every phase rather than for the new one.

    Deliberately narrow. It only ever adds a column, never drops, renames or
    retypes one, and only when the column is nullable or has a default — so the
    worst case is a column full of nulls, which is what a row written before the
    column existed actually means. Anything beyond that is a migration and wants
    a migration tool.
    """
    from sqlalchemy import inspect, text

    added = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all just made it, with every column
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not (column.nullable or column.default is not None):
                logger.warning(
                    f"{table.name}.{column.name} is missing and cannot be added "
                    f"safely — it is NOT NULL with no default"
                )
                continue
            kind = column.type.compile(engine.dialect)
            with engine.begin() as connection:
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {kind}')
                )
            added.append(f"{table.name}.{column.name}")
            logger.info(f"Added missing column {table.name}.{column.name}")

    return added


def init_database(env: str = "local") -> tuple[sessionmaker, object]:
    """Initialize database and return session factory and engine."""
    db_url = get_database_url(env)

    # Create engine
    if db_url.startswith("sqlite"):
        engine = create_engine(db_url, echo=False)
    else:
        engine = create_engine(db_url, echo=False, pool_pre_ping=True)

    # Create tables
    Base.metadata.create_all(engine)
    add_missing_columns(engine)

    # Create session factory
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    return SessionLocal, engine


def get_session(SessionLocal: sessionmaker) -> Session:
    """Get a database session."""
    return SessionLocal()
