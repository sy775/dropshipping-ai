"""Registre des fournisseurs. Ajouter un fournisseur = un module dans llm/providers + une entrée ici,
puis le nommer dans config/agents.yaml. Le reste du code n'a pas à changer."""
from __future__ import annotations

from typing import Callable

from llm.providers.base import Provider
from llm.providers.fake import FakeProvider

_FACTORIES: dict[str, Callable[[], Provider]] = {
    "fake": FakeProvider,
    # "anthropic": session 2 (llm/providers/anthropic.py)
}


class UnknownProviderError(LookupError):
    pass


def register(name: str, factory: Callable[[], Provider]) -> None:
    _FACTORIES[name] = factory


def create_provider(name: str) -> Provider:
    try:
        factory = _FACTORIES[name]
    except KeyError:
        raise UnknownProviderError(
            f"fournisseur non disponible : {name!r} (disponibles : {', '.join(sorted(_FACTORIES))})"
        ) from None
    return factory()
