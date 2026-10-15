"""Forward non-streaming chat completions to the configured provider."""

import json
import logging

import httpx
from flask import Response, current_app, request

from lgw.models import RequestStatus, UsageSource
from lgw.proxy.errors import (
    classify_upstream_status,
    error_response,
    gateway_error_response,
    invalid_json,
    invalid_model,
    passthrough_headers,
)
from lgw.proxy.estimate import estimate_tokens, output_text_from_response
from lgw.proxy.providers.deepseek import (
    UsageParseError,
    chat_completions_url,
    models_url,
    outbound_headers,
    parse_usage,
    rewrite_body,
)
from lgw.proxy.recorder import Outcome, finish_request, start_request

logger = logging.getLogger(__name__)

MODEL_MAX_LENGTH = 255
_READ_ERRORS = (
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)
_CONNECT_TIMEOUTS = (httpx.ConnectTimeout, httpx.WriteTimeout)


def forward_chat(api_key):
    """Validate the chat request and forward it.

    Invalid bodies are rejected before a request record is inserted.

    Parameters
    ----------
    api_key : AuthenticatedKey
        Key that passed authentication.

    Returns
    -------
    flask.Response or tuple
        Response for the client.
    """
    body = _json_object()
    if body is None:
        return invalid_json()
    model = body.get("model")
    if (
        "model" not in body
        or not isinstance(model, str)
        or len(model) > MODEL_MAX_LENGTH
    ):
        return invalid_model()
    provider = current_app.extensions["lgw_provider"]
    sessions = current_app.extensions["lgw_sessions"]
    try:
        record_id = start_request(
            sessions,
            key_id=api_key.id,
            provider=provider.name,
            model=model,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("failed to insert request")
        return gateway_error_response()
    stream = body.get("stream") is True
    result = _call_upstream(record_id, api_key, provider, body, stream)
    if isinstance(result[0], str):
        from lgw.proxy.streaming import streaming_response

        return streaming_response(sessions, record_id, result[1], body)
    outcome, client_response = result
    finish_request(sessions, record_id, outcome)
    return client_response


def _json_object():
    try:
        body = json.loads(request.get_data())
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    return body


def _call_upstream(record_id, api_key, provider, body, stream):
    payload = rewrite_body(body, api_key.user_sub)
    headers = outbound_headers(provider.api_key, request.headers, stream)
    client = current_app.extensions["lgw_http"]
    upstream_request = client.build_request(
        "POST",
        chat_completions_url(provider.base_url),
        headers=headers,
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    try:
        upstream = client.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        return _send_failure(exc, body)
    if stream and upstream.status_code == 200:
        return "live", upstream
    try:
        return _consume(record_id, upstream, body)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("gateway error for request %s", record_id)
        return _gateway_outcome(), gateway_error_response()
    finally:
        upstream.close()


def _consume(record_id, upstream, body):
    try:
        payload = upstream.read()
    except _READ_ERRORS:
        return _interrupted(body)
    if upstream.status_code != 200:
        return _upstream_status(record_id, upstream, payload)
    return _completed(record_id, upstream, payload, body)


def _upstream_status(record_id, upstream, payload):
    outcome = Outcome(
        RequestStatus.UPSTREAM_ERROR,
        UsageSource.REPORTED,
        0,
        0,
        0,
    )
    kind, code, message = classify_upstream_status(upstream.status_code)
    if kind == "passthrough":
        headers = passthrough_headers(upstream.status_code, upstream.headers)
        return outcome, Response(
            payload, status=upstream.status_code, headers=headers
        )
    logger.error(
        "upstream status %s for request %s body %s",
        upstream.status_code,
        record_id,
        payload.decode("utf-8", errors="replace")[:2000],
    )
    return outcome, error_response(message, "upstream_error", code, 502)


def _completed(record_id, upstream, payload, body):
    usage = _reported_usage(record_id, payload)
    if usage is None:
        logger.warning("request %s completed without usage", record_id)
        tokens = _estimated_tokens(body, payload)
        source = UsageSource.ESTIMATED
    else:
        tokens = usage
        source = UsageSource.REPORTED
    content_type = upstream.headers.get("content-type", "application/json")
    response = Response(
        payload, status=200, headers={"Content-Type": content_type}
    )
    return (
        Outcome(RequestStatus.COMPLETED, source, *tokens),
        response,
    )


def _reported_usage(record_id, payload):
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return parse_usage(parsed.get("usage"))
    except UsageParseError:
        logger.warning("request %s usage could not be parsed", record_id)
        return None


def _send_failure(exc, body):
    if isinstance(exc, _CONNECT_TIMEOUTS):
        return (
            _gateway_outcome(),
            error_response(
                "upstream connection timed out",
                "upstream_error",
                "upstream_timeout",
                504,
            ),
        )
    if isinstance(exc, _READ_ERRORS):
        return _interrupted(body)
    return (
        _gateway_outcome(),
        error_response(
            "upstream unavailable",
            "upstream_error",
            "upstream_unavailable",
            502,
        ),
    )


def _interrupted(body):
    tokens = _estimated_tokens(body, b"")
    return (
        Outcome(
            RequestStatus.UPSTREAM_INTERRUPTED, UsageSource.ESTIMATED, *tokens
        ),
        error_response(
            "upstream connection interrupted",
            "upstream_error",
            "upstream_interrupted",
            502,
        ),
    )


def _gateway_outcome():
    return Outcome(RequestStatus.GATEWAY_ERROR, UsageSource.UNKNOWN, 0, 0, 0)


def forward_models():
    """Forward ``GET /v1/models`` without writing a request record.

    Returns
    -------
    flask.Response or tuple
        Upstream body on success, or a mapped error response.
    """
    provider = current_app.extensions["lgw_provider"]
    client = current_app.extensions["lgw_http"]
    headers = {"Authorization": f"Bearer {provider.api_key}"}
    accept = request.headers.get("Accept")
    if accept:
        headers["Accept"] = accept
    upstream_request = client.build_request(
        "GET", models_url(provider.base_url), headers=headers
    )
    try:
        upstream = client.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        return _send_failure(exc, {})[1]
    try:
        try:
            payload = upstream.read()
        except _READ_ERRORS:
            return _interrupted(b"")[1]
    finally:
        upstream.close()
    if upstream.status_code != 200:
        return _upstream_status("models", upstream, payload)[1]
    content_type = upstream.headers.get("content-type", "application/json")
    return Response(
        payload, status=200, headers={"Content-Type": content_type}
    )


def _estimated_tokens(request_body, response_body):
    output_text = output_text_from_response(response_body)
    return estimate_tokens(request_body, output_text)
