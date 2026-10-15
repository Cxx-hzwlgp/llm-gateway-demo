"""Configuration loading shared by the admin and proxy applications."""

import json
import os
import re
from dataclasses import dataclass

DEFAULT_CONFIG_PATH = "config.json"


class ConfigurationError(Exception):
    """The configuration cannot be used to start an application."""


def load_config(path=None):
    """Load the JSON configuration file.

    Parameters
    ----------
    path : str, optional
        Path of the configuration file. Defaults to the
        ``CONFIG_FILE_PATH`` environment variable, then ``config.json``.

    Returns
    -------
    dict
        Parsed configuration.
    """
    path = path or os.environ.get("CONFIG_FILE_PATH", DEFAULT_CONFIG_PATH)
    with open(path, encoding="utf-8") as config_file:
        return json.load(config_file)


def database_url(config):
    """Return the configured SQLAlchemy database URL.

    Parameters
    ----------
    config : dict
        Parsed configuration.

    Returns
    -------
    str
        Database URL.

    Raises
    ------
    ConfigurationError
        ``database.url`` is missing or not a string.
    """
    database = config.get("database")
    if not isinstance(database, dict):
        raise ConfigurationError("database.url is required")
    url = database.get("url")
    if not isinstance(url, str) or not url:
        raise ConfigurationError("database.url is required")
    return url


@dataclass(frozen=True)
class ProxySettings:
    """Timeouts for upstream calls and the sweep cutoff."""

    max_request_seconds: float
    connect_timeout: float
    read_timeout: float


@dataclass(frozen=True)
class ProviderSettings:
    """The single upstream provider selected for this process."""

    name: str
    provider_type: str
    base_url: str
    api_key: str


_PROVIDER_NAME = re.compile(r"[a-z0-9-]+")
_PROVIDER_NAME_MAX = 64
_KNOWN_PROVIDER_TYPES = {"deepseek"}


def proxy_settings(config):
    """Return proxy timeouts, using the stage 1 defaults when omitted.

    Parameters
    ----------
    config : dict
        Parsed configuration.

    Returns
    -------
    ProxySettings
        Timeouts in seconds.

    Raises
    ------
    ConfigurationError
        A timeout is present but not a positive number.
    """
    proxy = config.get("proxy", {})
    if proxy is None:
        proxy = {}
    if not isinstance(proxy, dict):
        raise ConfigurationError("proxy settings must be an object")
    return ProxySettings(
        max_request_seconds=_positive_number(
            proxy, "max_request_seconds", 1800
        ),
        connect_timeout=_positive_number(
            proxy, "upstream_connect_timeout", 10
        ),
        read_timeout=_positive_number(proxy, "upstream_read_timeout", 120),
    )


def selected_provider(config):
    """Return the only configured provider.

    Parameters
    ----------
    config : dict
        Parsed configuration.

    Returns
    -------
    ProviderSettings
        Provider name, type, base URL and outbound API key.

    Raises
    ------
    ConfigurationError
        The provider count, name or type is not valid for stage 1.
    """
    providers = config.get("providers")
    if not isinstance(providers, dict) or len(providers) != 1:
        raise ConfigurationError("exactly one provider is required")
    name, spec = next(iter(providers.items()))
    if (
        not isinstance(name, str)
        or len(name) > _PROVIDER_NAME_MAX
        or _PROVIDER_NAME.fullmatch(name) is None
    ):
        raise ConfigurationError(
            "provider name must match [a-z0-9-]+ and be at most 64 characters"
        )
    if not isinstance(spec, dict):
        raise ConfigurationError("provider settings must be an object")
    if spec.get("type") not in _KNOWN_PROVIDER_TYPES:
        raise ConfigurationError("unknown provider type")
    base_url = spec.get("base_url")
    api_key = spec.get("api_key")
    if not isinstance(base_url, str) or not base_url:
        raise ConfigurationError("provider base_url is required")
    if not isinstance(api_key, str) or not api_key:
        raise ConfigurationError("provider api_key is required")
    return ProviderSettings(
        name=name,
        provider_type=spec["type"],
        base_url=base_url.rstrip("/"),
        api_key=api_key,
    )


def _positive_number(section, name, default):
    if name not in section:
        return float(default)
    value = section[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a positive number")
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive number")
    return float(value)
