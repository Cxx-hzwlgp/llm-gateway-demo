"""DeepSeek request rewriting and usage parsing."""

import pytest

from lgw.proxy.providers.deepseek import (
    ChunkNotes,
    UsageParseError,
    note_payload,
    outbound_headers,
    parse_usage,
    rewrite_body,
)


def test_user_id_is_replaced_and_stream_options_stay():
    body = {
        "model": "deepseek-chat",
        "user_id": "from-client",
        "stream_options": {"include_usage": True},
        "base_url": "http://127.0.0.1",
    }
    payload = rewrite_body(body, "user-1")
    assert payload["user_id"] == "user-1"
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["base_url"] == "http://127.0.0.1"
    assert body["user_id"] == "from-client"


def test_outbound_headers_replace_authorization():
    inbound = {
        "Authorization": "Bearer lgw-secret",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Request-Id": "trace",
    }
    headers = outbound_headers("sk-upstream", inbound, stream=False)
    assert headers == {
        "Authorization": "Bearer sk-upstream",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def test_usage_mapping():
    usage = {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "prompt_cache_hit_tokens": 3,
        "prompt_cache_miss_tokens": 8,
    }
    assert parse_usage(usage) == (11, 7, 3)
    assert parse_usage(None) is None


def test_null_usage_does_not_replace_a_saved_value():
    notes = ChunkNotes()
    note_payload(
        notes,
        {
            "choices": [{"delta": {"content": "hi"}}],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 1,
                "prompt_cache_hit_tokens": 2,
            },
        },
    )
    note_payload(
        notes,
        {"choices": [{"delta": {"content": "!"}}], "usage": None},
    )
    assert notes.usage == (4, 1, 2)
    assert notes.parts == ["hi", "!"]


def test_tool_call_fragments_are_accumulated():
    notes = ChunkNotes()
    note_payload(
        notes,
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{"q"',
                                }
                            }
                        ]
                    }
                }
            ]
        },
    )
    note_payload(
        notes,
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"function": {"arguments": ':"x"}'}}]
                    }
                }
            ]
        },
    )
    assert notes.parts == ["lookup", '{"q"', ':"x"}']


def test_invalid_payload_shapes_do_not_raise():
    notes = ChunkNotes()
    note_payload(notes, {"usage": {"prompt_tokens": "nope"}})
    note_payload(notes, {"choices": "bad", "usage": None})
    note_payload(notes, {"choices": [{"delta": {"tool_calls": "bad"}}]})
    assert notes.usage is None
    assert notes.parts == []


def test_invalid_usage_raises():
    with pytest.raises(UsageParseError):
        parse_usage(["nope"])
    with pytest.raises(UsageParseError):
        parse_usage({"prompt_tokens": 1})
    with pytest.raises(UsageParseError):
        parse_usage({"prompt_tokens": True, "completion_tokens": 1})
    with pytest.raises(UsageParseError):
        parse_usage({"prompt_tokens": -1, "completion_tokens": 1})
