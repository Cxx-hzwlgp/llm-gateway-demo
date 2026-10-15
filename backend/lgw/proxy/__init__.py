"""Proxy application: authenticate gateway keys and forward LLM requests."""

import httpx
from flask import Flask

from lgw.cli import register_cli
from lgw.config import (
    database_url,
    load_config,
    proxy_settings,
    selected_provider,
)
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
    loaded = config if config is not None else load_config()
    app.config["LGW"] = loaded
    provider = selected_provider(loaded)
    settings = proxy_settings(loaded)
    engine = create_engine_from_url(database_url(loaded))
    app.extensions["lgw_engine"] = engine
    app.extensions["lgw_sessions"] = create_session_factory(engine)
    app.extensions["lgw_provider"] = provider
    app.extensions["lgw_proxy"] = settings
    app.extensions["lgw_http"] = _upstream_client(settings)
    register_cli(app)
    register_routes(app)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


def _upstream_client(settings):
    timeout = httpx.Timeout(
        connect=settings.connect_timeout,
        read=settings.read_timeout,
        write=settings.connect_timeout,
        pool=settings.connect_timeout,
    )
    return httpx.Client(timeout=timeout, follow_redirects=False)
