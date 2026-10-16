"""Integration tests against a gunicorn proxy and a fake upstream."""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from openai import APIError, OpenAI
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lgw.db import create_engine_from_url
from lgw.keys import generate_key, hash_key, key_hint
from lgw.models import ApiKey, Base, KeyStatus, Request, User, utcnow
from lgw.proxy import create_app
from lgw.proxy.estimate import estimate_tokens
from tests.fake_upstream import tool_events, usage_events
from tests.support import proxy_config

_BACKEND = Path(__file__).resolve().parents[1]
_TOOL_TEXT = 'lookup{"q":1}'


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(url, proc, log_path):
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(log_path.read_text()[-4000:])
        try:
            httpx.get(url, timeout=0.5)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(log_path.read_text()[-4000:])


def _spawn(args, env, log_path):
    log_file = log_path.open("w")
    proc = subprocess.Popen(
        args,
        cwd=_BACKEND,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    return proc, log_file


def _stop(proc, log_file):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    log_file.close()


@pytest.fixture(scope="module")
def gateway(tmp_path_factory):
    """Start the fake upstream and the proxy for this module."""
    root = tmp_path_factory.mktemp("gateway")
    state = root / "state"
    state.mkdir()
    database = root / "lgw.db"
    database_url = f"sqlite:///{database}"
    upstream_port = _free_port()
    proxy_port = _free_port()
    config = proxy_config(database_url, f"http://127.0.0.1:{upstream_port}")
    config["proxy"] = {
        "max_request_seconds": 1800,
        "upstream_connect_timeout": 2,
        "upstream_read_timeout": 1,
    }
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config))
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "FAKE_UPSTREAM_STATE": str(state),
        "PYTHONUNBUFFERED": "1",
    }
    upstream_log = root / "upstream.log"
    upstream, upstream_file = _spawn(
        [
            sys.executable,
            "-m",
            "gunicorn",
            "-k",
            "gevent",
            "-w",
            "1",
            "-b",
            f"127.0.0.1:{upstream_port}",
            "tests.fake_upstream:app",
        ],
        env,
        upstream_log,
    )
    proxy = None
    proxy_file = None
    try:
        _wait(
            f"http://127.0.0.1:{upstream_port}/healthz", upstream, upstream_log
        )
        app = create_app(config)
        Base.metadata.create_all(app.extensions["lgw_engine"])
        raw_key, disabled = _seed_keys(app)
        app.extensions["lgw_http"].close()
        proxy_log = root / "proxy.log"
        proxy_env = dict(env)
        proxy_env["CONFIG_FILE_PATH"] = str(config_path)
        proxy, proxy_file = _spawn(
            [
                sys.executable,
                "-m",
                "gunicorn",
                "-k",
                "gevent",
                "-w",
                "1",
                "-b",
                f"127.0.0.1:{proxy_port}",
                "lgw.proxy:create_app()",
            ],
            proxy_env,
            proxy_log,
        )
        _wait(f"http://127.0.0.1:{proxy_port}/healthz", proxy, proxy_log)
    except Exception:
        if proxy is not None:
            _stop(proxy, proxy_file)
        _stop(upstream, upstream_file)
        raise
    base = f"http://127.0.0.1:{proxy_port}"
    client = httpx.Client(timeout=10)
    info = SimpleNamespace(
        base=base,
        key=raw_key,
        disabled=disabled,
        database_url=database_url,
        config=config,
        state=state,
        client=client,
    )
    try:
        yield info
    finally:
        client.close()
        _stop(proxy, proxy_file)
        _stop(upstream, upstream_file)


_MODEL = "deepseek-flash"


def _seed_keys(app):
    raw_key = generate_key()
    disabled = generate_key()
    with app.extensions["lgw_sessions"]() as session:
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
        session.add(
            ApiKey(
                user_sub="user-1",
                name="old",
                key_hash=hash_key(disabled),
                key_hint=key_hint(disabled),
                created_at=utcnow(),
                last_used_at=None,
                status=KeyStatus.DISABLED,
            )
        )
        session.commit()
    return raw_key, disabled


def _max_id(database_url):
    engine = create_engine_from_url(database_url)
    try:
        with Session(engine) as session:
            value = session.scalar(select(func.max(Request.id)))
        return 0 if value is None else value
    finally:
        engine.dispose()


def _record(database_url, model):
    engine = create_engine_from_url(database_url)
    try:
        with Session(engine) as session:
            row = session.scalars(
                select(Request).where(Request.model == model)
            ).one_or_none()
            if row is None:
                return None
            return SimpleNamespace(
                status=row.status,
                usage_source=row.usage_source,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_hit_tokens=row.cache_hit_tokens,
            )
    finally:
        engine.dispose()


def _record_after(database_url, after_id):
    engine = create_engine_from_url(database_url)
    try:
        with Session(engine) as session:
            row = session.scalars(
                select(Request)
                .where(Request.id > after_id)
                .order_by(Request.id)
            ).one_or_none()
            if row is None:
                return None
            return SimpleNamespace(
                status=row.status,
                usage_source=row.usage_source,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_hit_tokens=row.cache_hit_tokens,
            )
    finally:
        engine.dispose()


def _wait_after(database_url, after_id):
    deadline = time.time() + 5
    found = None
    while time.time() < deadline:
        found = _record_after(database_url, after_id)
        if found is not None and found.status != "in_progress":
            return found
        time.sleep(0.05)
    return found


def _count(database_url):
    engine = create_engine_from_url(database_url)
    try:
        with Session(engine) as session:
            return session.scalar(select(func.count()).select_from(Request))
    finally:
        engine.dispose()


def _post(gateway, mode, *, stream=False, extra=None):
    after_id = _max_id(gateway.database_url)
    body = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": "hello"}],
        "test_mode": mode,
    }
    if stream:
        body["stream"] = True
    if extra:
        body.update(extra)
    response = gateway.client.post(
        f"{gateway.base}/chat/completions",
        headers={"Authorization": f"Bearer {gateway.key}"},
        json=body,
    )
    return after_id, response


def test_nonstream_reports_usage(gateway):
    after_id, response = _post(gateway, "nonstream")
    assert response.status_code == 200
    record = _record_after(gateway.database_url, after_id)
    assert record.status == "completed"
    assert record.usage_source == "reported"
    assert (
        record.input_tokens,
        record.output_tokens,
        record.cache_hit_tokens,
    ) == (
        3,
        2,
        1,
    )


@pytest.mark.parametrize(
    ("mode", "events"),
    [("stream", usage_events(False)), ("stream_include", usage_events(True))],
)
def test_stream_bytes_match_upstream(gateway, mode, events):
    after_id = _max_id(gateway.database_url)
    expected = b"".join(events)
    with gateway.client.stream(
        "POST",
        f"{gateway.base}/chat/completions",
        headers={"Authorization": f"Bearer {gateway.key}"},
        json={
            "model": _MODEL,
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
            "test_mode": mode,
        },
    ) as response:
        assert response.status_code == 200
        body = b"".join(response.iter_bytes())
    assert body == expected
    record = _wait_after(gateway.database_url, after_id)
    assert record.status == "completed"
    assert record.usage_source == "reported"


def test_stream_tools_bytes_match(gateway):
    after_id = _max_id(gateway.database_url)
    with gateway.client.stream(
        "POST",
        f"{gateway.base}/chat/completions",
        headers={"Authorization": f"Bearer {gateway.key}"},
        json={
            "model": _MODEL,
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
            "test_mode": "stream_tools",
        },
    ) as response:
        body = b"".join(response.iter_bytes())
    assert body == b"".join(tool_events())
    record = _wait_after(gateway.database_url, after_id)
    assert record.status == "completed"
    assert record.usage_source == "reported"


def test_client_disconnect_closes_upstream(gateway):
    after_id = _max_id(gateway.database_url)
    state_key = uuid4().hex
    with gateway.client.stream(
        "POST",
        f"{gateway.base}/chat/completions",
        headers={"Authorization": f"Bearer {gateway.key}"},
        json={
            "model": _MODEL,
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
            "test_mode": "stream_slow",
            "state_key": state_key,
        },
    ) as response:
        assert response.status_code == 200
        next(response.iter_bytes())
    record = _wait_after(gateway.database_url, after_id)
    assert record.status == "client_interrupted"
    assert record.usage_source == "estimated"
    minimum = estimate_tokens({}, _TOOL_TEXT)[1]
    assert record.output_tokens >= minimum
    assert record.cache_hit_tokens == 0
    deadline = time.time() + 3
    path = gateway.state / state_key
    while time.time() < deadline and not path.exists():
        time.sleep(0.05)
    assert path.read_text() == "closed"


def test_upstream_cut_sends_an_error_event(gateway):
    after_id, response = _post(gateway, "stream_cut", stream=True)
    assert response.status_code == 200
    assert b"upstream_interrupted" in response.content
    assert b"[DONE]" not in response.content
    record = _wait_after(gateway.database_url, after_id)
    assert record.status == "upstream_interrupted"
    assert record.usage_source == "estimated"


def test_stream_without_done_is_interrupted(gateway):
    after_id, response = _post(gateway, "stream_no_done", stream=True)
    assert b'"code": "upstream_interrupted"' in response.content
    record = _record_after(gateway.database_url, after_id)
    assert record.status == "upstream_interrupted"


def test_read_timeout_is_interrupted(gateway):
    after_id, response = _post(gateway, "stream_hang", stream=True)
    assert b"upstream_interrupted" in response.content
    record = _wait_after(gateway.database_url, after_id)
    assert record.status == "upstream_interrupted"
    assert record.usage_source == "estimated"


def test_stream_without_usage_is_estimated(gateway):
    after_id, response = _post(gateway, "stream_no_usage", stream=True)
    assert response.content.endswith(b"data: [DONE]")
    record = _record_after(gateway.database_url, after_id)
    assert record.status == "completed"
    assert record.usage_source == "estimated"
    assert record.output_tokens == estimate_tokens({}, "hi")[1]


def test_invalid_json_event_is_forwarded(gateway):
    after_id, response = _post(gateway, "stream_bad_json", stream=True)
    assert response.content.startswith(b"data: not-json\n\n")
    record = _record_after(gateway.database_url, after_id)
    assert record.status == "completed"
    assert record.usage_source == "reported"


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("status-401", "upstream_auth_error"),
        ("status-402", "upstream_account_error"),
    ],
)
def test_upstream_account_errors_are_hidden(gateway, mode, code):
    after_id, response = _post(gateway, mode)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == code
    assert "account-secret" not in response.text
    record = _record_after(gateway.database_url, after_id)
    assert record.status == "upstream_error"
    assert record.usage_source == "reported"
    assert record.input_tokens == 0


@pytest.mark.parametrize("mode", ["status-400", "status-429", "status-500"])
def test_upstream_status_is_forwarded(gateway, mode):
    after_id, response = _post(gateway, mode)
    code = int(mode.split("-", 1)[1])
    assert response.status_code == code
    assert response.content == f"status-{code}".encode()
    if code == 429:
        assert response.headers["Retry-After"] == "7"
    record = _record_after(gateway.database_url, after_id)
    assert record.status == "upstream_error"


def test_invalid_and_disabled_keys_write_nothing(gateway):
    before = _count(gateway.database_url)
    missing = gateway.client.post(
        f"{gateway.base}/chat/completions",
        headers={"Authorization": "Bearer lgw-not-a-real-key"},
        json={"model": "m", "messages": []},
    )
    disabled = gateway.client.post(
        f"{gateway.base}/chat/completions",
        headers={"Authorization": f"Bearer {gateway.disabled}"},
        json={"model": "m", "messages": []},
    )
    assert missing.status_code == 401
    assert disabled.status_code == 401
    assert _count(gateway.database_url) == before


def test_sweep_marks_a_stale_request(gateway):
    engine = create_engine_from_url(gateway.database_url)
    with Session(engine) as session:
        key_id = session.scalars(select(ApiKey.id)).first()
        session.add(
            Request(
                key_id=key_id,
                provider="deepseek",
                model="stale-sweep",
                started_at=utcnow().replace(year=utcnow().year - 1),
                ended_at=None,
                status="in_progress",
                usage_source="unknown",
                input_tokens=9,
                output_tokens=9,
                cache_hit_tokens=9,
            )
        )
        session.commit()
    engine.dispose()
    app = create_app(gateway.config)
    try:
        result = app.test_cli_runner().invoke(args=["sweep"])
    finally:
        app.extensions["lgw_http"].close()
    assert result.exit_code == 0
    record = _record(gateway.database_url, "stale-sweep")
    assert record.status == "gateway_error"
    assert record.usage_source == "unknown"
    assert record.input_tokens == 0


def test_openai_sdk_streams_and_raises_on_error_events(gateway):
    client = OpenAI(base_url=gateway.base, api_key=gateway.key, timeout=10)
    reply = client.chat.completions.create(
        model=_MODEL,
        messages=[{"role": "user", "content": "hello"}],
        extra_body={"test_mode": "nonstream"},
    )
    assert reply.choices[0].message.content == "ok"
    streamed = client.chat.completions.create(
        model=_MODEL,
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        extra_body={"test_mode": "stream"},
    )
    chunks = list(streamed)
    assert chunks[-1].choices[0].finish_reason == "stop"
    with pytest.raises(APIError):
        list(
            client.chat.completions.create(
                model=_MODEL,
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                extra_body={"test_mode": "stream_no_done"},
            )
        )
