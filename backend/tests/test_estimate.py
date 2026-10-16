"""Token estimates for interrupted requests."""

import json

from lgw.proxy.estimate import (
    estimate_tokens,
    load_encoder,
    output_text_from_response,
)


def _count(text):
    return len(load_encoder().encode_ordinary(text))


def test_messages_include_text_parts_tools_and_calls():
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    body = {
        "model": "deepseek-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image_url", "image_url": {"url": "http://x"}},
                ],
            },
            {
                "role": "assistant",
                "content": "a",
                "reasoning_content": "b",
                "tool_calls": [
                    {"function": {"name": "lookup", "arguments": "{}"}}
                ],
            },
        ],
        "tools": tools,
    }
    text = "userhello" + "assistant" + "ab" + "lookup{}"
    text += json.dumps(tools, ensure_ascii=False)
    assert estimate_tokens(body, "out") == (_count(text), _count("out"), 0)


def test_special_token_literal_and_empty_output():
    body = {"messages": [{"role": "user", "content": "<|endoftext|>"}]}
    estimated = estimate_tokens(body, "")
    assert estimated[0] == _count("user<|endoftext|>")
    assert estimated[1:] == (0, 0)
    assert estimate_tokens({"messages": []}, "") == (0, 0, 0)


def test_non_streaming_response_text():
    raw = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": "hi",
                        "reasoning_content": "think",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{"q":1}',
                                }
                            }
                        ],
                    }
                }
            ]
        }
    ).encode()
    assert output_text_from_response(raw) == 'hithinklookup{"q":1}'
    assert output_text_from_response(b"not-json") == ""
    assert output_text_from_response(b"") == ""
