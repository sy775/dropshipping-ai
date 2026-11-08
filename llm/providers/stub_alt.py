"""Second fournisseur, simulé et sans réseau (« stub-alt »).

Sert à prouver qu'un agent change de fournisseur par la seule configuration, et de second modèle pour le
runner d'évaluation. Il n'a rien d'un vrai modèle : il refuse donc tout appel `purpose="prod"`.
Réponses déterministes : objet minimal conforme au schéma, sinon un texte fixe.
"""
from __future__ import annotations

import json
import math
from typing import Any

from llm.pricing import Usage
from llm.providers.base import BatchItemResult, ProviderError, ProviderRequest, ProviderResponse
from llm.providers.fake import dummy_from_schema

TEXT = "[stub-alt] réponse simulée"


class StubAltProvider:
    name = "stub_alt"

    def __init__(self, options: dict[str, Any] | None = None, alert_once: Any = None):
        self._batches: dict[str, list[tuple[str, ProviderRequest]]] = {}

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        if request.purpose == "prod":
            raise ProviderError("stub_alt : fournisseur de test, purpose=prod refusé", retryable=False)
        text = json.dumps(dummy_from_schema(request.schema)) if request.schema else TEXT
        chars = len(json.dumps(request.messages, ensure_ascii=False)) + len(request.system or "")
        return ProviderResponse(text=text, usage=Usage(tokens_in=math.ceil(chars / 4),
                                                       tokens_out=math.ceil(len(text) / 4)),
                                stop_reason="end_turn")

    def submit_batch(self, items: list[tuple[str, ProviderRequest]]) -> str:
        if any(req.purpose == "prod" for _, req in items):
            raise ProviderError("stub_alt : fournisseur de test, purpose=prod refusé", retryable=False)
        batch_id = f"stubbatch_{len(self._batches) + 1}"
        self._batches[batch_id] = list(items)
        return batch_id

    def batch_results(self, batch_id: str) -> list[BatchItemResult] | None:
        if batch_id not in self._batches:
            raise ProviderError(f"stub_alt : lot inconnu {batch_id}", retryable=False)
        return [BatchItemResult(cid, self.complete(req)) for cid, req in self._batches[batch_id]]
