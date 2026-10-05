import ipaddress
import os
import re
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url


REPO_ROOT = Path(__file__).resolve().parents[2]
collect_ignore = ["live"]


def validate_test_database_url(raw_url: str) -> str:
    try:
        url = make_url(raw_url)
        if url.drivername not in {"postgresql", "postgresql+psycopg"}:
            raise RuntimeError("test database must use PostgreSQL")
        if set(url.query) - {"dbname", "host", "port"}:
            raise RuntimeError("unsupported PostgreSQL connection options")
        positional, connection = url.get_dialect()().create_connect_args(url)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("invalid PostgreSQL connection settings") from exc

    if positional or set(connection) - {"dbname", "host", "port"}:
        raise RuntimeError("unsupported PostgreSQL connection options")
    database = connection.get("dbname")
    if not isinstance(database, str) or not re.fullmatch(r"scientist_(?:b[0-9]+|test(?:_[a-z0-9]+)?)", database):
        raise RuntimeError("database must use a dedicated test database name")
    host = connection.get("host")
    local_socket = isinstance(host, str) and host.startswith("/")
    local_host = host in {"localhost", "127.0.0.1", "::1"}
    if host and not local_host:
        try:
            local_host = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local_host = False
    if not local_socket and not local_host:
        raise RuntimeError("test database must use a local PostgreSQL socket or loopback host")
    port = connection.get("port")
    if port is not None and (not str(port).isdigit() or not 1 <= int(port) <= 65535):
        raise RuntimeError("test PostgreSQL port is invalid")
    return raw_url


test_database_url = os.environ.get("SCIENTIST_TEST_DATABASE_URL") or (
    f"postgresql+psycopg:///scientist_b1?host={REPO_ROOT / '.local/test-postgres/socket'}&port=54329"
)
os.environ["SCIENTIST_DATABASE_URL"] = validate_test_database_url(test_database_url)

from scientist.db import create_project, create_session, migrate, session


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    migrate()


@pytest.fixture
def db(migrated_database):
    with session() as db:
        yield db
        db.rollback()


@pytest.fixture
def project_session(db):
    project_id = create_project(db, "test project")
    session_id = create_session(db, project_id, "test session")
    return project_id, session_id


class _AnyProvider(dict):
    """Test default: every provider id resolves to the fixture destination."""

    def get(self, key, default=None):
        return "https://research.example"


@pytest.fixture(autouse=True)
def _configured_recipients(monkeypatch):
    from scientist import settings
    # Plans may only name configured destinations; cover the hosts the fixtures use.
    monkeypatch.setenv("SCIENTIST_SCHOLARLY_ENDPOINTS", "https://alternate.example,https://packages.example")
    monkeypatch.setattr(settings, "provider_destinations", lambda: _AnyProvider())


@pytest.fixture(autouse=True)
def _reset_dispatch_inactivity_proof():
    # supervisor.configure registers a process-global proof that outranks in-process dispatch history.
    yield
    from scientist import broker
    broker.configure_dispatch_inactivity(None)
    broker._inactive_dispatches.clear()
