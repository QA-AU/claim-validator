"""Database initialization and session management."""

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

logger = logging.getLogger(__name__)

AZURE_POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def _use_azure_ad_auth() -> bool:
    """Whether the Postgres connection authenticates via Azure AD (the
    Container App's own managed identity) instead of a password.

    Opt-in and separate from CLAIMVAL_DB_URL itself: infra/tenant.bicep's
    Azure AD mode gives the app a connection string with a username and
    no password (there is no password to have — the server has
    passwordAuth disabled entirely), so this needs an explicit signal
    rather than inferring it from the URL's shape.
    """
    return os.getenv("CLAIMVAL_DB_AAD_AUTH", "").strip().lower() in ("1", "true", "yes")


def _apply_azure_ad_token(credential, cparams: dict) -> None:
    """Sets a freshly fetched access token as the connection password.

    Split out from the do_connect listener below so the actual
    fetch-and-inject step is testable without SQLAlchemy's engine and
    event machinery in the way — this is the one line that matters, and
    the one line worth being able to call directly with a fake credential.
    """
    cparams["password"] = credential.get_token(AZURE_POSTGRES_SCOPE).token


def _install_azure_ad_token_provider(engine) -> None:
    """Fetches a fresh Azure AD access token on every new physical
    connection, not once at engine-creation time — a pooled connection
    can easily outlive a token issued when the engine was built (Azure AD
    Postgres tokens last roughly an hour), and Postgres accepts the token
    as a plain password at connect time, so intercepting SQLAlchemy's
    do_connect event is the only hook this needs.

    DefaultAzureCredential resolves to the Container App's own
    system-assigned managed identity in Azure, and falls back to
    `az login` locally for anyone testing this path by hand.
    """
    from azure.identity import DefaultAzureCredential
    from sqlalchemy import event

    credential = DefaultAzureCredential()

    @event.listens_for(engine, "do_connect")
    def _provide_token(dialect, conn_rec, cargs, cparams):
        _apply_azure_ad_token(credential, cparams)


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
    elif _use_azure_ad_auth():
        # pool_recycle keeps a connection from outliving its own token by
        # more than half the ~60-minute lifetime Azure AD Postgres tokens
        # get — do_connect (below) already self-heals on the next
        # reconnect regardless, since pool_pre_ping's liveness check fails
        # a connection whose token expired and SQLAlchemy discards it;
        # this just makes the refresh proactive rather than failure-driven.
        engine = create_engine(db_url, echo=False, pool_pre_ping=True, pool_recycle=1800)
        _install_azure_ad_token_provider(engine)
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
