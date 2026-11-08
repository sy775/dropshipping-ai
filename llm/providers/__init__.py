"""Registre des fournisseurs. Ajouter un fournisseur = un module dans llm/providers + une entrée ici,
puis le nommer dans config/agents.yaml. Le reste du code n'a pas à changer.

Une fabrique reçoit les options du fournisseur (section `providers.<nom>` de agents.yaml) et `alert_once`,
fourni par le runtime : alert_once(clé, texte) envoie une alerte Telegram au plus une fois par jour et par clé.
"""
from __future__ import annotations

from typing import Any, Callable

from llm.providers.base import Provider
from llm.providers.fake import FakeProvider

AlertOnce = Callable[[str, str], None]
Factory = Callable[[dict[str, Any], AlertOnce], Provider]


def _anthropic(options: dict[str, Any], alert_once: AlertOnce) -> Provider:
    from llm.providers.anthropic import AnthropicProvider   # import tardif : SDK chargé seulement si utilisé
    return AnthropicProvider(options, alert_once)


def _stub_alt(options: dict[str, Any], alert_once: AlertOnce) -> Provider:
    from llm.providers.stub_alt import StubAltProvider
    return StubAltProvider(options, alert_once)


_FACTORIES: dict[str, Factory] = {
    "fake": lambda options, alert_once: FakeProvider(),
    "anthropic": _anthropic,
    "stub_alt": _stub_alt,     # fournisseur de test sans réseau ; refuse purpose=prod
}


class UnknownProviderError(LookupError):
    pass


def register(name: str, factory: Factory) -> None:
    _FACTORIES[name] = factory


def _no_alert(key: str, text: str) -> None:
    import logging
    logging.getLogger(__name__).warning("[alerte sans runtime] %s", text)


def create_provider(name: str, options: dict[str, Any] | None = None,
                    alert_once: AlertOnce | None = None) -> Provider:
    try:
        factory = _FACTORIES[name]
    except KeyError:
        raise UnknownProviderError(
            f"fournisseur non disponible : {name!r} (disponibles : {', '.join(sorted(_FACTORIES))})"
        ) from None
    return factory(dict(options or {}), alert_once or _no_alert)
