"""Configuration loading shared by the admin and proxy applications."""

import json
import os

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
