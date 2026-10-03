from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Iterator
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from scientist.settings import DATABASE_URL


MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "001_initial.sql"
_engine: Engine | None = None
_sessions: sessionmaker[Session] | None = None


def engine() -> Engine:
    global _engine, _sessions
    if _engine is None:
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        _sessions = sessionmaker(_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session() -> Iterator[Session]:
    if _sessions is None:
        engine()
    assert _sessions is not None
    db = _sessions()
    try:
        yield db
    finally:
        db.close()


def migrate(database: Engine | None = None, migration_path: Path = MIGRATION) -> None:
    content = migration_path.read_bytes()
    digest = sha256(content).hexdigest()
    with (database or engine()).begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('scientist.schema_migrations'))"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, checksum char(64) NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"))
        applied = conn.execute(text("SELECT checksum FROM schema_migrations WHERE version = '001_initial'")).scalar_one_or_none()
        if applied is not None:
            if applied.strip() != digest:
                raise RuntimeError("applied migration checksum mismatch for 001_initial")
            return
        conn.exec_driver_sql(content.decode("utf-8"))
        conn.execute(text("INSERT INTO schema_migrations (version, checksum) VALUES ('001_initial', :checksum)"), {"checksum": digest})


def create_project(db: Session, name: str) -> UUID:
    project_id = uuid4()
    db.execute(text("INSERT INTO projects (id, name) VALUES (:id, :name)"), {"id": project_id, "name": name})
    return project_id


def create_session(db: Session, project_id: UUID, title: str) -> UUID:
    session_id = uuid4()
    db.execute(text("INSERT INTO sessions (id, project_id, title) VALUES (:id, :project_id, :title)"), {"id": session_id, "project_id": project_id, "title": title})
    return session_id


def reserve_tokens(db: Session, run_id: UUID, amount: int) -> None:
    if amount < 0:
        raise ValueError("reservation must be nonnegative")
    reserved = db.execute(text("UPDATE runs SET reserved_tokens = reserved_tokens + :amount WHERE id = :id AND usage_tokens + reserved_tokens + :amount <= token_limit RETURNING id"), {"amount": amount, "id": run_id}).scalar_one_or_none()
    if reserved is None:
        raise ValueError("budget exhausted or run not found")


def settle_tokens(db: Session, run_id: UUID, reservation: int, actual_usage: int) -> None:
    if reservation < 0 or actual_usage < 0:
        raise ValueError("token values must be nonnegative")
    settled = db.execute(text("UPDATE runs SET reserved_tokens = reserved_tokens - :reservation, usage_tokens = usage_tokens + :usage WHERE id = :id AND reserved_tokens >= :reservation RETURNING id"), {"reservation": reservation, "usage": actual_usage, "id": run_id}).scalar_one_or_none()
    if settled is None:
        raise ValueError("reservation not found")
