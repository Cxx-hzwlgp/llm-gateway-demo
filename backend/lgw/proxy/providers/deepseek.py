"""DeepSeek request rewriting and usage parsing."""


class UsageParseError(Exception):
    """A usage object was present but did not match the expected fields."""


def chat_completions_url(base_url):
    """Return the chat completions URL for ``base_url``.

    Parameters
    ----------
    base_url : str
        Provider base URL without a trailing path.

    Returns
    -------
    str
        Absolute chat completions URL.
    """
    return base_url.rstrip("/") + "/chat/completions"


def rewrite_body(body, user_sub):
    """Return a request body whose ``user_id`` is ``user_sub``.

    Other fields, including ``stream_options``, are copied unchanged.
    The upstream URL is not read from the body.

    Parameters
    ----------
    body : dict
        Parsed client JSON object.
    user_sub : str
        Gateway user subject.

    Returns
    -------
    dict
        Body to send upstream.
    """
    payload = dict(body)
    payload["user_id"] = user_sub
    return payload


def outbound_headers(api_key, inbound_headers, stream):
    """Return the headers sent to DeepSeek.

    Only ``Content-Type`` and ``Accept`` are copied from the client.
    ``Authorization`` is always the outbound provider key.

    Parameters
    ----------
    api_key : str
        Outbound provider API key.
    inbound_headers : Mapping
        Client request headers.
    stream : bool
        Whether the client asked for a stream. Used only when the
        client did not send ``Accept``.

    Returns
    -------
    dict
        Headers for the upstream request.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    content_type = inbound_headers.get("Content-Type")
    headers["Content-Type"] = content_type or "application/json"
    accept = inbound_headers.get("Accept")
    if accept:
        headers["Accept"] = accept
    elif stream:
        headers["Accept"] = "text/event-stream"
    else:
        headers["Accept"] = "application/json"
    return headers


def parse_usage(usage):
    """Map a DeepSeek usage object to gateway token counts.

    Parameters
    ----------
    usage : dict or None
        DeepSeek ``usage`` value. ``None`` means the field was null or
        absent.

    Returns
    -------
    tuple of int or None
        ``(input_tokens, output_tokens, cache_hit_tokens)``, or None
        when ``usage`` is None.

    Raises
    ------
    UsageParseError
        ``usage`` is not null and does not contain the required counts.
    """
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise UsageParseError("usage is not an object")
    try:
        input_tokens = _token_count(usage["prompt_tokens"])
        output_tokens = _token_count(usage["completion_tokens"])
        cache_hit = usage.get("prompt_cache_hit_tokens", 0)
        if cache_hit is None:
            cache_hit = 0
        cache_hit_tokens = _token_count(cache_hit)
    except (KeyError, TypeError, ValueError) as exc:
        raise UsageParseError("usage fields are invalid") from exc
    return input_tokens, output_tokens, cache_hit_tokens


def _token_count(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(value)
    if value < 0:
        raise ValueError(value)
    return value
