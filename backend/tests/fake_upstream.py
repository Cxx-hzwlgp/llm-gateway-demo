"""Fake DeepSeek server used by the integration tests."""

import json
import os
import time
from pathlib import Path

from flask import Flask, Response, request

app = Flask(__name__)

_USAGE = {
    "prompt_tokens": 3,
    "completion_tokens": 2,
    "prompt_cache_hit_tokens": 1,
}


def sse(payload):
    """Return one SSE event for ``payload``."""
    if isinstance(payload, bytes):
        body = payload
    else:
        body = json.dumps(payload).encode()
    return b"data: " + body + b"\n\n"


def usage_events(include_null):
    """Return a normal usage stream, optionally with a null usage chunk."""
    first = {"choices": [{"delta": {"content": "hi"}}]}
    if include_null:
        first["usage"] = None
    last = {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": _USAGE,
    }
    return [sse(first), sse(last), b"data: [DONE]\n\n"]


def tool_events():
    """Return a stream that carries a tool call and then usage."""
    first = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "lookup",
                                "arguments": '{"q":1}',
                            }
                        }
                    ]
                }
            }
        ]
    }
    last = {
        "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        "usage": _USAGE,
    }
    return [sse(first), sse(last), b"data: [DONE]\n\n"]


@app.get("/healthz")
def healthz():
    """Report that the fake upstream is accepting connections."""
    return {"status": "ok"}


@app.get("/models")
def models():
    """Return the models the integration proxy is allowed to route."""
    return {
        "object": "list",
        "data": [
            {"id": "deepseek-flash", "object": "model"},
            {"id": "deepseek-v4-pro", "object": "model"},
        ],
    }


@app.post("/chat/completions")
def chat():
    """Return the behavior selected by ``test_mode``."""
    body = request.get_json(force=True, silent=True) or {}
    mode = body.get("test_mode", "nonstream")
    if mode.startswith("status-"):
        return _status(int(mode.split("-", 1)[1]))
    events = _events(mode, body)
    if events is None:
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": _USAGE,
        }
    return Response(
        _generate(mode, body, events), mimetype="text/event-stream"
    )


def _status(code):
    headers = {"Content-Type": "text/plain"}
    if code == 429:
        headers["Retry-After"] = "7"
    text = "account-secret" if code in (401, 402) else f"status-{code}"
    return Response(text, status=code, headers=headers)


def _events(mode, body):
    if mode == "stream":
        return usage_events(False)
    if mode == "stream_include":
        return usage_events(True)
    if mode == "stream_tools":
        return tool_events()
    if mode == "stream_no_usage":
        return [
            sse({"choices": [{"delta": {"content": "hi"}}]}),
            b"data: [DONE]",
        ]
    if mode == "stream_bad_json":
        return [b"data: not-json\n\n", *usage_events(False)]
    if mode == "stream_no_done":
        return [sse({"choices": [{"delta": {"content": "hi"}}]})]
    if mode in {"stream_cut", "stream_hang", "stream_slow"}:
        return []
    return None


def _generate(mode, body, events):
    if mode == "stream_hang":
        yield b": keep-alive\n\n"
        time.sleep(5)
        return
    if mode == "stream_cut":
        yield sse({"choices": [{"delta": {"content": "hi"}}]})
        sock = request.environ.get("gunicorn.socket")
        if sock is not None:
            sock.close()
        return
    if mode == "stream_slow":
        yield from _slow(body)
        return
    yield from events


def _slow(body):
    directory = Path(os.environ["FAKE_UPSTREAM_STATE"])
    path = directory / body["state_key"]
    try:
        yield sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":1}',
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        )
        for _ in range(40):
            yield sse({"choices": [{"delta": {"content": "x"}}]})
            time.sleep(0.05)
    except GeneratorExit:
        path.write_text("closed")
        raise
