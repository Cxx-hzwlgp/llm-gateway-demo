"""Proxy configuration and startup model catalog."""

import httpx
import pytest
import respx

from lgw.config import ConfigurationError
from lgw.proxy import create_app
from tests.support import models_document, open_app, proxy_config


def _config(tmp_path):
    return proxy_config(f"sqlite:///{tmp_path / 'lgw.db'}")


def test_provider_models_are_loaded_at_startup(tmp_path):
    seen = {}

    def _capture(upstream_request):
        seen["authorization"] = upstream_request.headers["authorization"]
        return httpx.Response(
            200,
            json=models_document(("deepseek-flash", "deepseek-v4-pro")),
        )

    config = _config(tmp_path)
    with respx.mock:
        respx.get("https://upstream.test/models").mock(side_effect=_capture)
        app = create_app(config)
    catalog = app.extensions["lgw_catalog"]
    assert seen["authorization"] == "Bearer sk-upstream"
    assert catalog.routes["deepseek-flash"] == ("deepseek",)
    assert catalog.provider_for("deepseek-flash").name == "deepseek"
    assert catalog.routes["deepseek-v4-pro"] == ("deepseek",)
    assert [item["id"] for item in catalog.models] == [
        "deepseek-flash",
        "deepseek-v4-pro",
    ]
    app.extensions["lgw_http"].close()


def test_two_providers_keep_distinct_models(tmp_path):
    config = _config(tmp_path)
    config["providers"]["other"] = {
        "base_url": "https://other.test",
        "api_key": "sk-other",
    }
    app = open_app(
        config,
        {
            "https://upstream.test": ("deepseek-flash",),
            "https://other.test": ("kimi-k2",),
        },
    )
    catalog = app.extensions["lgw_catalog"]
    assert catalog.routes["deepseek-flash"] == ("deepseek",)
    assert catalog.routes["kimi-k2"] == ("other",)
    assert catalog.provider_for("kimi-k2").api_key == "sk-other"
    assert [item["id"] for item in catalog.models] == [
        "deepseek-flash",
        "kimi-k2",
    ]
    app.extensions["lgw_http"].close()


def test_shared_model_keeps_every_provider_and_defaults_to_the_first(
    tmp_path,
):
    config = _config(tmp_path)
    config["providers"]["other"] = {
        "base_url": "https://other.test",
        "api_key": "sk-other",
    }
    app = open_app(
        config,
        {
            "https://upstream.test": ("deepseek-flash", "deepseek-flash"),
            "https://other.test": ("deepseek-flash",),
        },
    )
    catalog = app.extensions["lgw_catalog"]
    assert catalog.routes["deepseek-flash"] == ("deepseek", "other")
    assert catalog.provider_for("deepseek-flash").name == "deepseek"
    assert [item["id"] for item in catalog.models] == ["deepseek-flash"]
    app.extensions["lgw_http"].close()


@pytest.mark.parametrize(
    ("status", "body"),
    [(401, b"secret"), (200, b"not-json"), (200, b'{"data":[{"id":1}]}')],
)
def test_invalid_model_list_fails_startup(tmp_path, status, body):
    config = _config(tmp_path)
    with respx.mock:
        respx.get("https://upstream.test/models").mock(
            return_value=httpx.Response(status, content=body)
        )
        with pytest.raises(ConfigurationError):
            create_app(config)


@pytest.mark.parametrize(
    "mutate",
    ["none", "name", "long_name", "base_url", "api_key", "timeout"],
)
def test_invalid_provider_config_fails(tmp_path, mutate):
    config = _config(tmp_path)
    if mutate == "none":
        config["providers"] = {}
    elif mutate == "name":
        config["providers"] = {"DeepSeek": config["providers"]["deepseek"]}
    elif mutate == "long_name":
        config["providers"] = {"a" * 65: config["providers"]["deepseek"]}
    elif mutate == "base_url":
        config["providers"]["deepseek"]["base_url"] = ""
    elif mutate == "api_key":
        config["providers"]["deepseek"]["api_key"] = ""
    else:
        config["proxy"] = {"upstream_read_timeout": 0}
    with pytest.raises(ConfigurationError):
        create_app(config)
