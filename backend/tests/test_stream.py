"""Streaming chat completions and interruption records."""

import json

import httpx
from sqlalchemy import select

from lgw.models import ApiKey
from lgw.proxy.estimate import estimate_tokens
from lgw.proxy.recorder import start_request
from lgw.proxy.streaming import streaming_response
from tests.support import create_proxy_app
from tests.test_forward import _insert_key, _records

_ERROR_EVENT = (
    b'data: {"error": {"message": "upstream connection interrupted", '
    b'"type": "upstream_error", "code": "upstream_interrupted"}}\n\n'
)

_USAGE = {
    "prompt_tokens": 5,
    "completion_tokens": 1,
    "prompt_cache_hit_tokens": 0,
}


class _Upstream:
    def __init__(self, chunks, error=None):
        self._chunks = list(chunks)
        self._error = error
        self.closed = False
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}

    def iter_bytes(self):
        yield from self._chunks
        if self._error is not None:
            raise self._error

    def close(self):
        self.closed = True


def _event(payload):
    if isinstance(payload, bytes):
        data = payload
    else:
        data = json.dumps(payload).encode()
    return b"data: " + data + b"\n\n"


def _post(app, raw_key, body):
    return app.test_client().post(
        "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {raw_key}",
            "Content-Type": "application/json",
        },
    )


def _install(app, upstream, seen):
    def _send(upstream_request, stream=False):
        del stream
        seen["accept"] = upstream_request.headers["accept"]
        seen["authorization"] = upstream_request.headers["authorization"]
        seen["body"] = json.loads(upstream_request.content)
        return upstream

    app.extensions["lgw_http"].send = _send


def test_stream_bytes_match_and_usage_is_reported(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    first = {
        "choices": [{"delta": {"content": "hi"}, "finish_reason": None}],
        "usage": None,
    }
    last = {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": _USAGE,
    }
    raw = _event(first) + _event(last) + b"data: [DONE]\n\n"
    upstream = _Upstream([raw])
    seen = {}
    _install(app, upstream, seen)
    response = _post(
        app,
        raw_key,
        {
            "model": "deepseek-chat",
            "stream": True,
            "user_id": "from-client",
            "stream_options": {"include_usage": True},
        },
    )
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.get_data() == raw
    assert seen["authorization"] == "Bearer sk-upstream"
    assert seen["body"]["user_id"] == "user-1"
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert seen["accept"] == "text/event-stream"
    record = _records(app)[0]
    assert record.status == "completed"
    assert record.usage_source == "reported"
    assert record.input_tokens == 5
    assert upstream.closed
    app.extensions["lgw_http"].close()


def test_stream_without_done_appends_an_error_event(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    raw = _event({"choices": [{"delta": {"content": "hi"}}]})
    _install(app, _Upstream([raw]), {})
    response = _post(app, raw_key, {"model": "m", "stream": True})
    assert response.get_data() == raw + _ERROR_EVENT
    record = _records(app)[0]
    assert record.status == "upstream_interrupted"
    assert record.usage_source == "estimated"
    assert record.output_tokens == estimate_tokens({}, "hi")[1]
    assert record.cache_hit_tokens == 0
    app.extensions["lgw_http"].close()


def test_read_error_does_not_forward_a_partial_event(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    complete = _event({"choices": [{"delta": {"content": "hi"}}]})
    upstream = _Upstream(
        [complete, b"data: {"],
        error=httpx.ReadError("reset"),
    )
    _install(app, upstream, {})
    response = _post(app, raw_key, {"model": "m", "stream": True})
    assert response.get_data() == complete + _ERROR_EVENT
    record = _records(app)[0]
    assert record.status == "upstream_interrupted"
    assert upstream.closed
    app.extensions["lgw_http"].close()


def test_done_without_usage_is_estimated(tmp_path, caplog):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    raw = _event({"choices": [{"delta": {"content": "hi"}}]})
    raw += b"data: [DONE]"
    _install(app, _Upstream([raw]), {})
    response = _post(app, raw_key, {"model": "m", "stream": True})
    assert response.get_data() == raw
    record = _records(app)[0]
    assert record.status == "completed"
    assert record.usage_source == "estimated"
    assert record.output_tokens == estimate_tokens({}, "hi")[1]
    assert record.cache_hit_tokens == 0
    assert "without usage" in caplog.text
    app.extensions["lgw_http"].close()


def test_invalid_json_event_is_forwarded(tmp_path):
    app = create_proxy_app(tmp_path)
    raw_key = _insert_key(app)
    raw = b"data: not-json\n\n" + _event(
        {"choices": [{"delta": {}}], "usage": _USAGE}
    )
    raw += b"data: [DONE]\n\n"
    _install(app, _Upstream([raw]), {})
    response = _post(app, raw_key, {"model": "m", "stream": True})
    assert response.get_data() == raw
    record = _records(app)[0]
    assert record.status == "completed"
    assert record.usage_source == "reported"
    app.extensions["lgw_http"].close()


def test_client_disconnect_closes_upstream_and_estimates(tmp_path):
    app = create_proxy_app(tmp_path)
    _insert_key(app)
    sessions = app.extensions["lgw_sessions"]
    key_id = _records_key_id(app)
    record_id = start_request(
        sessions, key_id=key_id, provider="deepseek", model="m"
    )
    upstream = _Upstream(
        [
            _event({"choices": [{"delta": {"content": "hi"}}]}),
            _event({"choices": [{"delta": {"content": " there"}}]}),
        ]
    )
    request_body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hello"}],
    }
    response = streaming_response(sessions, record_id, upstream, request_body)
    generator = response.response
    assert next(generator).startswith(b"data: ")
    generator.close()
    assert upstream.closed
    record = _records(app)[0]
    assert record.status == "client_interrupted"
    assert record.usage_source == "estimated"
    assert (
        record.input_tokens,
        record.output_tokens,
        record.cache_hit_tokens,
    ) == estimate_tokens(request_body, "hi")
    app.extensions["lgw_http"].close()


def _records_key_id(app):
    sessions = app.extensions["lgw_sessions"]
    with sessions() as session:
        return session.scalars(select(ApiKey)).one().id
