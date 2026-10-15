"""Shared proxy configuration for tests."""

from lgw.models import Base
from lgw.proxy import create_app


def proxy_config(database_url, base_url="https://upstream.test"):
    return {
        "database": {"url": database_url},
        "providers": {
            "deepseek": {
                "type": "deepseek",
                "base_url": base_url,
                "api_key": "sk-upstream",
            }
        },
    }


def create_proxy_app(tmp_path, base_url="https://upstream.test"):
    database = tmp_path / "lgw.db"
    app = create_app(proxy_config(f"sqlite:///{database}", base_url))
    Base.metadata.create_all(app.extensions["lgw_engine"])
    return app
