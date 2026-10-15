"""Proxy application: authenticate gateway keys and forward LLM requests."""

from flask import Flask

from lgw.cli import register_cli
from lgw.config import database_url, load_config
from lgw.db import create_engine_from_url, create_session_factory
from lgw.proxy.routes import register_routes


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
    engine = create_engine_from_url(database_url(app.config["LGW"]))
    app.extensions["lgw_engine"] = engine
    app.extensions["lgw_sessions"] = create_session_factory(engine)
    register_cli(app)
    register_routes(app)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
