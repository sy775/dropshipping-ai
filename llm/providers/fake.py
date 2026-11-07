"""Fournisseur simulé : utilisé en dry-run (défaut) et dans les tests. Aucun appel réseau."""
from __future__ import annotations

import json
import math
import uuid
from typing import Any, Callable

from llm.pricing import Usage
from llm.providers.base import BatchItemResult, ProviderError, ProviderRequest, ProviderResponse

Responder = Callable[[ProviderRequest], ProviderResponse]


def dummy_from_schema(schema: dict[str, Any]) -> Any:
    """Plus petite valeur conforme à un JSON Schema simple (champs requis seulement)."""
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t == "object":
        props = schema.get("properties", {})
        return {k: dummy_from_schema(props.get(k, {})) for k in schema.get("required", [])}
    if t == "array":
        return [dummy_from_schema(schema.get("items", {})) for _ in range(schema.get("minItems", 0))]
    if t == "string":
        return "x" * schema.get("minLength", 0)
    if t in ("number", "integer"):
        return schema.get("minimum", 0)
    if t == "boolean":
        return False
    return None


def default_responder(request: ProviderRequest) -> ProviderResponse:
    text = json.dumps(dummy_from_schema(request.schema)) if request.schema else "[dry-run] réponse simulée"
    chars = len(json.dumps(request.messages, ensure_ascii=False))
    prefix = len(request.system or "")
    return ProviderResponse(
        text=text,
        usage=Usage(tokens_in=math.ceil(chars / 4), tokens_out=math.ceil(len(text) / 4),
                    cache_read=math.ceil(prefix / 4)),
        stop_reason="end_turn",
    )


class FakeProvider:
    name = "fake"

    def __init__(self, responder: Responder | None = None, failures: list[ProviderError] | None = None):
        self.responder = responder or default_responder
        self.failures = list(failures or [])   # erreurs levées, dans l'ordre, avant de répondre
        self.requests: list[ProviderRequest] = []
        self.batches: dict[str, list[tuple[str, ProviderRequest]]] = {}
        self.completed: set[str] = set()

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
        return self.responder(request)

    def submit_batch(self, items: list[tuple[str, ProviderRequest]]) -> str:
        if self.failures:
            raise self.failures.pop(0)
        batch_id = f"fakebatch_{uuid.uuid4().hex[:12]}"
        self.batches[batch_id] = list(items)
        return batch_id

    def finish(self, batch_id: str) -> None:
        """Simule la fin du traitement d'un lot côté fournisseur."""
        self.completed.add(batch_id)

    def batch_results(self, batch_id: str) -> list[BatchItemResult] | None:
        if batch_id not in self.batches:
            raise ProviderError(f"lot inconnu : {batch_id}", retryable=False)
        if batch_id not in self.completed:
            return None
        return [BatchItemResult(cid, self.responder(req)) for cid, req in self.batches[batch_id]]
