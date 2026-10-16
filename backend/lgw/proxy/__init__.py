"""Proxy application: authenticate gateway keys and forward LLM requests."""

import httpx
from flask import Flask

from lgw.cli import register_cli
from lgw.config import (
    database_url,
    load_config,
    load_providers,
    proxy_settings,
)
from lgw.db import create_engine_from_url, create_session_factory
from lgw.proxy.catalog import load_catalog
from lgw.proxy.estimate import load_encoder
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
    providers = load_providers(loaded)
    settings = proxy_settings(loaded)
    db_url = database_url(loaded)
    client = _upstream_client(settings)
    try:
        catalog = load_catalog(client, providers)
        load_encoder()
    except Exception:
        client.close()
        raise
    engine = create_engine_from_url(db_url)
    app.extensions["lgw_engine"] = engine
    app.extensions["lgw_sessions"] = create_session_factory(engine)
    app.extensions["lgw_catalog"] = catalog
    app.extensions["lgw_proxy"] = settings
    app.extensions["lgw_http"] = client
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
