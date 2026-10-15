"""Proxy application: authenticate gateway keys and forward LLM requests."""

from flask import Flask

from lgw.config import load_config


def create_app(config=None):
    """Create the proxy Flask application.

    Parameters
    ----------
    config : dict, optional
        Application configuration. Loaded from file when omitted.

    Returns
    -------
    Flask
        Initialized application.
    """
    app = Flask(__name__)
    app.config["LGW"] = config if config is not None else load_config()

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
