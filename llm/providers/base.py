"""Interface commune des fournisseurs LLM. L'adaptateur ne connaît que ces types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from llm.pricing import Usage


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    messages: list[dict[str, Any]]
    max_tokens: int
    system: str | None = None            # cache_prefix : partie stable, mise en cache
    schema: dict[str, Any] | None = None  # sortie structurée (JSON Schema)
    tools: list[dict[str, Any]] | None = None
    temperature: float | None = None     # None = valeur par défaut du modèle
    effort: str | None = None            # low | medium | high | xhigh | max ; None = défaut du modèle
    purpose: str = "prod"                # prod | eval : permet à un fournisseur de test de refuser la prod


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    usage: Usage
    # Motif d'arrêt normalisé (vocabulaire Messages API). Obligatoire, sans valeur par défaut : chaque
    # fournisseur (réel ou simulé) le renseigne explicitement. None est traité comme une réponse incomplète.
    stop_reason: str | None


@dataclass(frozen=True)
class BatchItemResult:
    custom_id: str
    response: ProviderResponse | None
    error: str | None = None


class ProviderError(Exception):
    """Erreur du fournisseur. retryable=True pour 429, 5xx, coupure réseau, délai dépassé."""

    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class Provider(Protocol):
    name: str

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...

    def submit_batch(self, items: list[tuple[str, ProviderRequest]]) -> str:
        """Envoie un lot ; renvoie l'identifiant du lot chez le fournisseur."""
        ...

    def batch_results(self, batch_id: str) -> list[BatchItemResult] | None:
        """Résultats du lot, ou None s'il n'est pas terminé."""
        ...
