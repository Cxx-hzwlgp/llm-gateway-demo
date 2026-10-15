"""Gateway key authentication."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from lgw.keys import generate_key, hash_key, key_hint
from lgw.models import ApiKey, KeyStatus, Request, User
from tests.support import create_proxy_app

_FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_ERROR = {
    "error": {
        "message": "invalid api key",
        "type": "invalid_request_error",
        "code": "invalid_api_key",
    }
}


def _app(tmp_path):
    return create_proxy_app(tmp_path)


def _insert_key(app, *, status=KeyStatus.ACTIVE, last_used_at=None):
    raw_key = generate_key()
    sessions = app.extensions["lgw_sessions"]
    with sessions() as session:
        session.add(User(sub="user-1", username="alice", email=None))
        session.flush()
        session.add(
            ApiKey(
                user_sub="user-1",
                name="laptop",
                key_hash=hash_key(raw_key),
                key_hint=key_hint(raw_key),
                created_at=_FIXED_NOW,
                last_used_at=last_used_at,
                status=status,
            )
        )
        session.commit()
    return raw_key


def _post(app, raw_key=None, header=None):
    headers = {}
    if header is not None:
        headers["Authorization"] = header
    elif raw_key is not None:
        headers["Authorization"] = f"Bearer {raw_key}"
    return app.test_client().post("/v1/chat/completions", headers=headers)


def _request_count(app):
    sessions = app.extensions["lgw_sessions"]
    with sessions() as session:
        return len(list(session.scalars(select(Request))))


def test_missing_or_malformed_key_is_401_without_a_record(tmp_path):
    app = _app(tmp_path)
    for header in (None, "Basic lgw-aaaaaaaa", "Bearer", "Bearer not-a-key"):
        response = _post(app, header=header)
        assert response.status_code == 401
        assert response.get_json() == _ERROR
    assert _request_count(app) == 0


def test_unknown_and_disabled_keys_match_the_missing_key_error(tmp_path):
    app = _app(tmp_path)
    disabled = _insert_key(app, status=KeyStatus.DISABLED)
    unknown = generate_key()
    for raw_key in (disabled, unknown):
        response = _post(app, raw_key)
        assert response.status_code == 401
        assert response.get_json() == _ERROR
    assert _request_count(app) == 0
    sessions = app.extensions["lgw_sessions"]
    with sessions() as session:
        stored = session.scalars(select(ApiKey)).one()
        assert stored.last_used_at is None


def test_active_key_is_accepted_and_touched(tmp_path, monkeypatch):
    monkeypatch.setattr("lgw.proxy.auth.utcnow", lambda: _FIXED_NOW)
    app = _app(tmp_path)
    raw_key = _insert_key(app)
    response = _post(app, raw_key)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_json"
    sessions = app.extensions["lgw_sessions"]
    with sessions() as session:
        stored = session.scalars(select(ApiKey)).one()
        assert stored.last_used_at.replace(tzinfo=UTC) == _FIXED_NOW
    again = _post(app, raw_key)
    assert again.status_code == 400
    with sessions() as session:
        stored = session.scalars(select(ApiKey)).one()
        assert stored.last_used_at.replace(tzinfo=UTC) == _FIXED_NOW


def test_last_used_write_failure_still_authenticates(tmp_path, caplog):
    app = _app(tmp_path)
    raw_key = _insert_key(app)
    sessions = app.extensions["lgw_sessions"]

    class _Session:
        def __enter__(self):
            self._session = sessions().__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self._session.__exit__(exc_type, exc, traceback)

        def scalar(self, statement):
            return self._session.scalar(statement)

        def commit(self):
            raise RuntimeError("locked")

        def rollback(self):
            self._session.rollback()

    app.extensions["lgw_sessions"] = _Session
    with caplog.at_level("ERROR"):
        response = _post(app, raw_key)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_json"
    assert "last_used_at" in caplog.text


def test_last_used_at_updates_only_after_one_minute(tmp_path, monkeypatch):
    monkeypatch.setattr("lgw.proxy.auth.utcnow", lambda: _FIXED_NOW)
    app = _app(tmp_path)
    recent = _FIXED_NOW - timedelta(minutes=1)
    raw_key = _insert_key(app, last_used_at=recent)
    assert _post(app, raw_key).status_code == 400
    sessions = app.extensions["lgw_sessions"]
    with sessions() as session:
        stored = session.scalars(select(ApiKey)).one()
        assert stored.last_used_at.replace(tzinfo=UTC) == recent

    stale = _FIXED_NOW - timedelta(minutes=1, seconds=1)
    with sessions() as session:
        stored = session.scalars(select(ApiKey)).one()
        stored.last_used_at = stale
        session.commit()
    assert _post(app, raw_key).status_code == 400
    with sessions() as session:
        stored = session.scalars(select(ApiKey)).one()
        assert stored.last_used_at.replace(tzinfo=UTC) == _FIXED_NOW
