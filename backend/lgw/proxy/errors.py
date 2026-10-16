"""OpenAI-format error responses and upstream status mapping."""

from flask import jsonify

_PASSTHROUGH_STATUSES = {400, 422, 429}


def error_response(message, error_type, code, status):
    """Return an OpenAI-format error response.

    Parameters
    ----------
    message : str
        Human-readable error message.
    error_type : str
        OpenAI ``error.type`` value.
    code : str
        OpenAI ``error.code`` value.
    status : int
        HTTP status code.

    Returns
    -------
    tuple
        Flask response and status code.
    """
    body = {"error": {"message": message, "type": error_type, "code": code}}
    return jsonify(body), status


def invalid_api_key():
    """Return the 401 response used for every authentication failure.

    Returns
    -------
    tuple
        Flask response and status code. The body does not say whether
        the key was missing, malformed, unknown or disabled.
    """
    return error_response(
        "invalid api key",
        "invalid_request_error",
        "invalid_api_key",
        401,
    )


def invalid_json():
    """Return the 400 response for a body that is not a JSON object.

    Returns
    -------
    tuple
        Flask response and status code.
    """
    return error_response(
        "request body must be a JSON object",
        "invalid_request_error",
        "invalid_json",
        400,
    )


def model_not_found():
    """Return the 404 response for a model that is not in the catalog.

    Returns
    -------
    tuple
        Flask response and status code.
    """
    return error_response(
        "model not found",
        "invalid_request_error",
        "model_not_found",
        404,
    )


def invalid_model():
    """Return the 400 response for a missing or invalid model name.

    Returns
    -------
    tuple
        Flask response and status code.
    """
    return error_response(
        "model must be a string of at most 255 characters",
        "invalid_request_error",
        "invalid_model",
        400,
    )


def gateway_error_response():
    """Return the 500 response for an unexpected gateway failure.

    Returns
    -------
    tuple
        Flask response and status code.
    """
    return error_response(
        "gateway error",
        "gateway_error",
        "gateway_error",
        500,
    )


def classify_upstream_status(status_code):
    """Decide how an upstream HTTP status is returned to the client.

    Parameters
    ----------
    status_code : int
        HTTP status from the upstream response.

    Returns
    -------
    tuple
        ``("passthrough", None, None)`` when the status and body are
        forwarded, or ``("reject", code, message)`` when the gateway
        answers 502 and keeps the upstream body in the log.
    """
    if status_code in _PASSTHROUGH_STATUSES or 500 <= status_code <= 599:
        return "passthrough", None, None
    if status_code in (401, 403):
        return (
            "reject",
            "upstream_auth_error",
            "upstream authentication failed",
        )
    if status_code == 402:
        return (
            "reject",
            "upstream_account_error",
            "upstream account error",
        )
    return "reject", "upstream_error", "upstream request failed"


def passthrough_headers(status_code, upstream_headers):
    """Return the upstream headers that may be sent to the client.

    Parameters
    ----------
    status_code : int
        Upstream HTTP status.
    upstream_headers : Mapping
        Upstream response headers.

    Returns
    -------
    dict
        ``Content-Type`` when present, plus ``Retry-After`` for 429.
    """
    headers = {}
    content_type = upstream_headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type
    if status_code == 429:
        retry_after = upstream_headers.get("retry-after")
        if retry_after:
            headers["Retry-After"] = retry_after
    return headers
