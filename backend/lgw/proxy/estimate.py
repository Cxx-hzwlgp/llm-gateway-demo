"""Estimate token counts when the upstream does not report usage.

Every model shares tiktoken's ``o200k_base`` encoding. The counts are
not billing tokens. The encoding is loaded when the app is created so
the first interrupted request does not pay for that load.
"""

import json

from lgw.proxy.providers.deepseek import text_from_delta


class _Encoder:  # pylint: disable=too-few-public-methods
    value = None


def load_encoder():
    """Load the shared tokenizer.

    Returns
    -------
    tiktoken.Encoding
        The ``o200k_base`` encoding.
    """
    if _Encoder.value is None:
        import tiktoken

        _Encoder.value = tiktoken.get_encoding("o200k_base")
    return _Encoder.value


def estimate_tokens(request_body, output_text):
    """Return estimated input, output and cache-hit token counts.

    Parameters
    ----------
    request_body : dict
        Parsed client request.
    output_text : str
        Assistant text already received. Empty when nothing arrived.

    Returns
    -------
    tuple of int
        ``(input_tokens, output_tokens, cache_hit_tokens)``. Cache hits
        are always zero.
    """
    return (_input_tokens(request_body), _count(output_text), 0)


def output_text_from_response(raw):
    """Return estimable assistant text from a non-streaming body.

    Parameters
    ----------
    raw : bytes
        Upstream response body. An empty or unreadable body contributes
        no output tokens.

    Returns
    -------
    str
        Concatenated message text.
    """
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return ""
    parts = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            parts.append(text_from_delta(message))
    return "".join(parts)


def _input_tokens(request_body):
    if not isinstance(request_body, dict):
        return 0
    parts = []
    messages = request_body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                parts.append(_message_text(message))
    if "tools" in request_body and request_body["tools"] is not None:
        parts.append(json.dumps(request_body["tools"], ensure_ascii=False))
    return _count("".join(parts))


def _message_text(message):
    parts = []
    role = message.get("role")
    if isinstance(role, str):
        parts.append(role)
    parts.append(_content_text(message.get("content")))
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str):
        parts.append(reasoning)
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            parts.append(_function_text(call))
    return "".join(parts)


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _function_text(call):
    if not isinstance(call, dict):
        return ""
    function = call.get("function")
    if not isinstance(function, dict):
        return ""
    parts = []
    name = function.get("name")
    arguments = function.get("arguments")
    if isinstance(name, str):
        parts.append(name)
    if isinstance(arguments, str):
        parts.append(arguments)
    elif arguments is not None:
        parts.append(json.dumps(arguments, ensure_ascii=False))
    return "".join(parts)


def _count(text):
    if not text:
        return 0
    return len(load_encoder().encode_ordinary(text))
