"""Proxy HTTP routes."""

from lgw.proxy.auth import AuthenticatedKey, authenticate
from lgw.proxy.errors import error_response


def register_routes(app):
    """Register forwarding routes on ``app``.

    Parameters
    ----------
    app : flask.Flask
        Proxy application.
    """

    @app.post("/v1/chat/completions")
    def chat_completions():
        """Authenticate the caller before the request is forwarded."""
        result = authenticate()
        if not isinstance(result, AuthenticatedKey):
            return result
        return error_response(
            "not implemented",
            "gateway_error",
            "not_implemented",
            501,
        )
