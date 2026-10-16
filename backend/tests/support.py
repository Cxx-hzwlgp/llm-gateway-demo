"""Shared proxy configuration for tests."""

import httpx
import respx

from lgw.models import Base
from lgw.proxy import create_app

DEFAULT_MODELS = ("deepseek-flash", "deepseek-v4-pro", "m")


def proxy_config(database_url, base_url="https://upstream.test"):
    return {
        "database": {"url": database_url},
        "providers": {
            "deepseek": {
                "base_url": base_url,
                "api_key": "sk-upstream",
            }
        },
    }


def models_document(model_ids):
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model"} for model_id in model_ids
        ],
    }


def open_app(config, models_by_url=None):
    if models_by_url is None:
        models_by_url = {
            spec["base_url"]: DEFAULT_MODELS
            for spec in config["providers"].values()
        }
    with respx.mock:
        for base_url, model_ids in models_by_url.items():
            respx.get(base_url.rstrip("/") + "/models").mock(
                return_value=httpx.Response(
                    200, json=models_document(model_ids)
                )
            )
        return create_app(config)


def create_proxy_app(tmp_path, base_url="https://upstream.test"):
    database = tmp_path / "lgw.db"
    app = open_app(proxy_config(f"sqlite:///{database}", base_url))
    Base.metadata.create_all(app.extensions["lgw_engine"])
    return app
