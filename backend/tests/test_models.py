"""GET /models returns the startup catalog."""

from sqlalchemy import select

from lgw.models import Request
from tests.support import create_proxy_app
from tests.test_forward import _insert_key


def _get(app, raw_key, **headers):
    extra = {"Authorization": f"Bearer {raw_key}"}
    extra.update(headers)
    return app.test_client().get("/models", headers=extra)


def _record_count(app):
    sessions = app.extensions["lgw_sessions"]
    with sessions() as session:
        return len(list(session.scalars(select(Request))))


def test_models_come_from_the_startup_catalog(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    response = _get(app, raw_key)
    assert response.status_code == 200
    assert response.get_json() == {
        "object": "list",
        "data": [
            {"id": "deepseek-flash", "object": "model"},
            {"id": "deepseek-v4-pro", "object": "model"},
            {"id": "m", "object": "model"},
        ],
    }
    assert _record_count(app) == 0
    missing = app.test_client().get("/models")
    assert missing.status_code == 401
    app.extensions["lgw_http"].close()
