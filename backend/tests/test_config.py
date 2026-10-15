"""Proxy configuration checks."""

import pytest

from lgw.config import ConfigurationError
from lgw.proxy import create_app
from tests.support import proxy_config


def _config(tmp_path):
    return proxy_config(f"sqlite:///{tmp_path / 'lgw.db'}")


def test_one_deepseek_provider_starts(tmp_path):
    app = create_app(_config(tmp_path))
    assert app.extensions["lgw_provider"].name == "deepseek"
    app.extensions["lgw_http"].close()


@pytest.mark.parametrize(
    "mutate",
    [
        "none",
        "two",
        "name",
        "long_name",
        "type",
        "base_url",
        "api_key",
        "timeout",
    ],
)
def test_invalid_provider_config_fails(tmp_path, mutate):
    config = _config(tmp_path)
    if mutate == "none":
        config["providers"] = {}
    elif mutate == "two":
        config["providers"]["other"] = dict(config["providers"]["deepseek"])
    elif mutate == "name":
        config["providers"] = {"DeepSeek": config["providers"]["deepseek"]}
    elif mutate == "long_name":
        config["providers"] = {"a" * 65: config["providers"]["deepseek"]}
    elif mutate == "type":
        config["providers"]["deepseek"]["type"] = "bedrock"
    elif mutate == "base_url":
        config["providers"]["deepseek"]["base_url"] = ""
    elif mutate == "api_key":
        config["providers"]["deepseek"]["api_key"] = ""
    else:
        config["proxy"] = {"upstream_read_timeout": 0}
    with pytest.raises(ConfigurationError):
        create_app(config)
