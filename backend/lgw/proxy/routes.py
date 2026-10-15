"""Proxy HTTP routes."""

from lgw.proxy.auth import AuthenticatedKey, authenticate
from lgw.proxy.forward import forward_chat, forward_models


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

    @app.get("/v1/models")
    def models():
        """Authenticate the caller and forward the model list."""
        result = authenticate()
        if not isinstance(result, AuthenticatedKey):
            return result
        return forward_models()
