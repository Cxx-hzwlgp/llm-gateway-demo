"""Non-streaming chat completion forwarding."""

import json

import httpx
import respx
from sqlalchemy import select

from lgw.keys import generate_key, hash_key, key_hint
from lgw.models import ApiKey, KeyStatus, Request, User, utcnow
from lgw.proxy.estimate import estimate_tokens
from tests.support import create_proxy_app

_URL = "https://upstream.test/chat/completions"


def _insert_key(app):
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
                created_at=utcnow(),
                last_used_at=None,
                status=KeyStatus.ACTIVE,
            )
        )
        session.commit()
    return raw_key


def _post(app, raw_key, body, **headers):
    extra = {
        "Authorization": f"Bearer {raw_key}",
        "Accept": "application/json",
    }
    extra.update(headers)
    return app.test_client().post(
        "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers=extra,
    )


def _records(app):
    sessions = app.extensions["lgw_sessions"]
    with sessions() as session:
        return list(session.scalars(select(Request)))


def test_nonstream_completion_reports_usage_and_hides_client_headers(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    upstream_body = (
        b'\n\n{"usage":{"prompt_tokens":11,"completion_tokens":7,'
        b'"prompt_cache_hit_tokens":3},"choices":[]}'
    )
    seen = {}

    def _capture(upstream_request):
        seen["authorization"] = upstream_request.headers["authorization"]
        seen["body"] = json.loads(upstream_request.content)
        seen["names"] = {name.lower() for name in upstream_request.headers}
        return httpx.Response(
            200,
            content=upstream_body,
            headers={"content-type": "application/json"},
        )

    with respx.mock:
        route = respx.post(_URL).mock(side_effect=_capture)
        response = _post(
            app,
            raw_key,
            {
                "model": "deepseek-chat",
                "user_id": "from-client",
                "stream_options": {"include_usage": True},
                "base_url": "http://127.0.0.1/secret",
                "messages": [{"role": "user", "content": "hi"}],
            },
            **{"X-Trace": "nope"},
        )
    assert route.called
    assert response.status_code == 200
    assert response.get_data() == upstream_body
    assert seen["authorization"] == "Bearer sk-upstream"
    assert "x-trace" not in seen["names"]
    assert seen["body"]["user_id"] == "user-1"
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert seen["body"]["base_url"] == "http://127.0.0.1/secret"
    record = _records(app)[0]
    assert record.status == "completed"
    assert record.usage_source == "reported"
    assert record.provider == "deepseek"
    assert record.model == "deepseek-chat"
    assert (
        record.input_tokens,
        record.output_tokens,
        record.cache_hit_tokens,
    ) == (
        11,
        7,
        3,
    )
    assert record.ended_at is not None
    app.extensions["lgw_http"].close()


def test_missing_usage_is_completed_and_estimated(tmp_path, caplog):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    body = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
    upstream = {
        "choices": [{"message": {"role": "assistant", "content": "world"}}]
    }
    with respx.mock:
        respx.post(_URL).mock(return_value=httpx.Response(200, json=upstream))
        response = _post(app, raw_key, body)
    assert response.status_code == 200
    record = _records(app)[0]
    assert record.status == "completed"
    assert record.usage_source == "estimated"
    assert (
        record.input_tokens,
        record.output_tokens,
        record.cache_hit_tokens,
    ) == estimate_tokens(body, "world")
    assert "completed without usage" in caplog.text
    app.extensions["lgw_http"].close()


def test_invalid_model_does_not_call_upstream(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    with respx.mock:
        route = respx.post(_URL).mock(return_value=httpx.Response(200))
        missing = _post(app, raw_key, {"messages": []})
        too_long = _post(app, raw_key, {"model": "m" * 256})
        not_string = _post(app, raw_key, {"model": 1})
    assert missing.status_code == 400
    assert missing.get_json()["error"]["code"] == "invalid_model"
    assert too_long.get_json()["error"]["code"] == "invalid_model"
    assert not_string.get_json()["error"]["code"] == "invalid_model"
    assert not route.called
    assert _records(app) == []
    app.extensions["lgw_http"].close()


def test_invalid_json_does_not_call_upstream(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    with respx.mock:
        route = respx.post(_URL).mock(return_value=httpx.Response(200))
        response = app.test_client().post(
            "/v1/chat/completions",
            data=b"[1, 2]",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_json"
    assert not route.called
    assert _records(app) == []
    app.extensions["lgw_http"].close()


def test_upstream_401_and_404_hide_the_body(tmp_path, caplog):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    secret = "account-secret"
    with respx.mock:
        respx.post(_URL).mock(
            return_value=httpx.Response(401, json={"error": secret})
        )
        denied = _post(app, raw_key, {"model": "m"})
    assert denied.status_code == 502
    assert denied.get_json()["error"]["code"] == "upstream_auth_error"
    assert secret not in denied.get_data(as_text=True)
    assert secret in caplog.text
    record = _records(app)[0]
    assert record.status == "upstream_error"
    assert record.usage_source == "reported"
    assert record.input_tokens == 0

    with respx.mock:
        respx.post(_URL).mock(
            return_value=httpx.Response(404, content=b"not-found-body")
        )
        missing = _post(app, raw_key, {"model": "m"})
    assert missing.status_code == 502
    assert missing.get_json()["error"]["code"] == "upstream_error"
    assert b"not-found-body" not in missing.get_data()
    app.extensions["lgw_http"].close()


def test_upstream_400_and_429_are_forwarded(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    with respx.mock:
        respx.post(_URL).mock(
            return_value=httpx.Response(
                400,
                content=b'{"error":"bad model"}',
                headers={"content-type": "application/json"},
            )
        )
        bad = _post(app, raw_key, {"model": "m"})
    assert bad.status_code == 400
    assert bad.get_data() == b'{"error":"bad model"}'
    with respx.mock:
        respx.post(_URL).mock(
            return_value=httpx.Response(
                429,
                content=b"slow down",
                headers={"content-type": "text/plain", "retry-after": "8"},
            )
        )
        limited = _post(app, raw_key, {"model": "m"})
    assert limited.status_code == 429
    assert limited.get_data() == b"slow down"
    assert limited.headers["Retry-After"] == "8"
    records = _records(app)
    assert [row.status for row in records] == [
        "upstream_error",
        "upstream_error",
    ]
    app.extensions["lgw_http"].close()


def test_connect_timeout_is_504_and_connect_error_is_502(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    with respx.mock:
        respx.post(_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
        timed_out = _post(app, raw_key, {"model": "m"})
    assert timed_out.status_code == 504
    assert timed_out.get_json()["error"]["code"] == "upstream_timeout"
    record = _records(app)[0]
    assert record.status == "gateway_error"
    assert record.usage_source == "unknown"

    with respx.mock:
        respx.post(_URL).mock(side_effect=httpx.ConnectError("down"))
        down = _post(app, raw_key, {"model": "m"})
    assert down.status_code == 502
    assert down.get_json()["error"]["code"] == "upstream_unavailable"
    app.extensions["lgw_http"].close()


def test_timeout_before_headers_is_a_gateway_timeout(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)

    def _send(upstream_request, stream=False):
        del upstream_request, stream
        raise httpx.ReadTimeout("slow")

    app.extensions["lgw_http"].send = _send
    body = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
    response = _post(app, raw_key, body)
    assert response.status_code == 504
    assert response.get_json()["error"]["code"] == "upstream_timeout"
    record = _records(app)[0]
    assert record.status == "gateway_error"
    assert record.usage_source == "unknown"
    assert record.input_tokens == 0
    app.extensions["lgw_http"].close()


def test_read_timeout_is_upstream_interrupted(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)

    class _Slow:
        status_code = 200
        headers = {}

        def read(self):
            raise httpx.ReadTimeout("slow")

        def close(self):
            return None

    def _send(upstream_request, stream=False):
        del upstream_request, stream
        return _Slow()

    app.extensions["lgw_http"].send = _send
    body = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
    response = _post(app, raw_key, body)
    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "upstream_interrupted"
    record = _records(app)[0]
    assert record.status == "upstream_interrupted"
    assert record.usage_source == "estimated"
    assert (
        record.input_tokens,
        record.output_tokens,
        record.cache_hit_tokens,
    ) == estimate_tokens(body, "")
    app.extensions["lgw_http"].close()
