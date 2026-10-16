"""SQLite pragmas, init-db and create-key."""

from sqlalchemy import select

from lgw.db import BUSY_TIMEOUT_MS, create_engine_from_url
from lgw.keys import hash_key, is_key_format
from lgw.models import ApiKey, KeyStatus, User
from tests.support import open_app, proxy_config


def _app(tmp_path):
    database = tmp_path / "lgw.db"
    return open_app(proxy_config(f"sqlite:///{database}"))


def test_sqlite_enables_wal_and_busy_timeout(tmp_path):
    database = tmp_path / "lgw.db"
    engine = create_engine_from_url(f"sqlite:///{database}")
    with engine.connect() as connection:
        mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
        foreign_keys = connection.exec_driver_sql(
            "PRAGMA foreign_keys"
        ).scalar()
    assert mode == "wal"
    assert timeout == BUSY_TIMEOUT_MS
    assert foreign_keys == 1


def test_init_db_and_create_key(tmp_path):
    app = _app(tmp_path)
    runner = app.test_cli_runner()
    initialized = runner.invoke(args=["init-db"])
    assert initialized.exit_code == 0

    created = runner.invoke(
        args=[
            "create-key",
            "--user-sub",
            "user-1",
            "--username",
            "alice",
            "--name",
            "laptop",
        ]
    )
    assert created.exit_code == 0
    raw_key = created.stdout.strip()
    assert is_key_format(raw_key)
    assert raw_key not in created.stderr

    sessions = app.extensions["lgw_sessions"]
    with sessions() as session:
        user = session.get(User, "user-1")
        assert user.username == "alice"
        assert user.email is None
        stored = session.scalars(select(ApiKey)).one()
        assert stored.key_hash == hash_key(raw_key)
        assert raw_key not in stored.key_hash
        assert stored.key_hint == f"lgw-{raw_key[4:8]}\u2026{raw_key[-4:]}"
        assert stored.status == KeyStatus.ACTIVE
        assert stored.name == "laptop"
        assert stored.last_used_at is None

    again = runner.invoke(
        args=[
            "create-key",
            "--user-sub",
            "user-1",
            "--username",
            "other",
            "--name",
            "phone",
        ]
    )
    assert again.exit_code == 0
    with sessions() as session:
        user = session.get(User, "user-1")
        assert user.username == "alice"
        assert len(list(session.scalars(select(ApiKey)))) == 2


def test_create_key_rejects_invalid_sub(tmp_path):
    app = _app(tmp_path)
    runner = app.test_cli_runner()
    runner.invoke(args=["init-db"])
    result = runner.invoke(
        args=[
            "create-key",
            "--user-sub",
            "bad sub",
            "--username",
            "alice",
            "--name",
            "laptop",
        ]
    )
    assert result.exit_code != 0
    sessions = app.extensions["lgw_sessions"]
    with sessions() as session:
        assert session.get(User, "bad sub") is None
