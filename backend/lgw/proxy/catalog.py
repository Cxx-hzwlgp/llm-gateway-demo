"""Model list loaded from each provider at process start."""

import json
from dataclasses import dataclass
from types import MappingProxyType

import httpx

from lgw.config import ConfigurationError
from lgw.proxy.providers.deepseek import models_url


@dataclass(frozen=True)
class ModelCatalog:
    """Models known at startup.

    ``routes`` maps each model id to the provider names that listed it,
    in configuration order. ``providers`` resolves those names.
    """

    providers: MappingProxyType
    routes: MappingProxyType
    models: tuple

    def provider_for(self, model):
        """Return the first provider that listed ``model``.

        Parameters
        ----------
        model : str
            Client model name.

        Returns
        -------
        ProviderSettings or None
            ``None`` when ``model`` is not in the catalog. When several
            providers listed it, this is the first name in ``routes``.
        """
        names = self.routes.get(model)
        if not names:
            return None
        return self.providers[names[0]]


def load_catalog(client, providers):
    """Fetch each provider's model list and index it by model id.

    Parameters
    ----------
    client : httpx.Client
        Shared upstream client.
    providers : sequence of ProviderSettings
        Providers in configuration order.

    Returns
    -------
    ModelCatalog
        Routes and the merged model list. A model listed by several
        providers keeps every provider name; the public model list
        contains that id once.

    Raises
    ------
    ConfigurationError
        A list cannot be read or is not the expected shape.
    """
    by_name = {}
    routes = {}
    listed = []
    for provider in providers:
        by_name[provider.name] = provider
        for model_id in _model_ids(client, provider):
            names = routes.get(model_id)
            if names is None:
                routes[model_id] = [provider.name]
                listed.append({"id": model_id, "object": "model"})
            elif provider.name not in names:
                names.append(provider.name)
    frozen = {model_id: tuple(names) for model_id, names in routes.items()}
    return ModelCatalog(
        providers=MappingProxyType(by_name),
        routes=MappingProxyType(frozen),
        models=tuple(listed),
    )


def _model_ids(client, provider):
    try:
        response = client.get(
            models_url(provider.base_url),
            headers={"Authorization": f"Bearer {provider.api_key}"},
        )
    except httpx.HTTPError as exc:
        raise ConfigurationError(
            f"{provider.name} model list request failed"
        ) from exc
    if response.status_code != 200:
        raise ConfigurationError(
            f"{provider.name} model list returned {response.status_code}"
        )
    return _ids_from_body(provider.name, response)


def _ids_from_body(provider_name, response):
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"{provider_name} model list is not JSON"
        ) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ConfigurationError(
            f"{provider_name} model list is missing a data array"
        )
    return [_model_id(provider_name, item) for item in data]


def _model_id(provider_name, item):
    model_id = item.get("id") if isinstance(item, dict) else None
    if not isinstance(model_id, str) or not model_id:
        raise ConfigurationError(
            f"{provider_name} model list contains an invalid id"
        )
    return model_id
