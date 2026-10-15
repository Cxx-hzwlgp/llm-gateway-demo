"""GET /v1/models passthrough."""

import httpx
import respx
from sqlalchemy import select

from lgw.models import Request
from tests.support import create_proxy_app
from tests.test_forward import _insert_key

_URL = "https://upstream.test/models"


def _get(app, raw_key, **headers):
    extra = {"Authorization": f"Bearer {raw_key}"}
    extra.update(headers)
    return app.test_client().get("/v1/models", headers=extra)


def _record_count(app):
    sessions = app.extensions["lgw_sessions"]
    with sessions() as session:
        return len(list(session.scalars(select(Request))))


def test_models_are_forwarded_without_a_request_record(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    seen = {}

    def _capture(upstream_request):
        seen["authorization"] = upstream_request.headers["authorization"]
        seen["accept"] = upstream_request.headers["accept"]
        assert "x-trace" not in {
            name.lower() for name in upstream_request.headers
        }
        return httpx.Response(
            200,
            content=b'{"data":[]}',
            headers={"content-type": "application/json"},
        )

    with respx.mock:
        route = respx.get(_URL).mock(side_effect=_capture)
        response = _get(
            app, raw_key, Accept="application/json", **{"X-Trace": "no"}
        )
    assert route.called
    assert response.status_code == 200
    assert response.get_data() == b'{"data":[]}'
    assert seen["authorization"] == "Bearer sk-upstream"
    assert seen["accept"] == "application/json"
    assert _record_count(app) == 0
    app.extensions["lgw_http"].close()


def test_models_errors_use_the_chat_mapping(tmp_path, caplog):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    with respx.mock:
        respx.get(_URL).mock(
            return_value=httpx.Response(401, content=b"account-secret")
        )
        denied = _get(app, raw_key)
    assert denied.status_code == 502
    assert denied.get_json()["error"]["code"] == "upstream_auth_error"
    assert b"account-secret" not in denied.get_data()
    assert "account-secret" in caplog.text
    with respx.mock:
        respx.get(_URL).mock(return_value=httpx.Response(400, content=b"bad"))
        bad = _get(app, raw_key)
    assert bad.status_code == 400
    assert bad.get_data() == b"bad"
    assert _record_count(app) == 0
    missing = app.test_client().get("/v1/models")
    assert missing.status_code == 401
    app.extensions["lgw_http"].close()
