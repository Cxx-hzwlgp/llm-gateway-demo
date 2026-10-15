"""Proxy HTTP routes."""

from lgw.proxy.auth import AuthenticatedKey, authenticate
from lgw.proxy.forward import forward_chat


def register_routes(app):
    """Register forwarding routes on ``app``.

    Parameters
    ----------
    app : flask.Flask
        Proxy application.
    """

    @app.post("/v1/chat/completions")
    def chat_completions():
        """Authenticate the caller and forward a chat completion."""
        result = authenticate()
        if not isinstance(result, AuthenticatedKey):
            return result
        return forward_chat(result)
