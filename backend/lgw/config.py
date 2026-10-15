"""Configuration loading shared by the admin and proxy applications."""

import json
import os

DEFAULT_CONFIG_PATH = "config.json"


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
