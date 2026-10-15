"""OpenAI-format error responses."""

from flask import jsonify


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
