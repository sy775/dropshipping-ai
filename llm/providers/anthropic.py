"""Fournisseur Anthropic (Messages API et Message Batches API), via le SDK Python officiel.

Références (consultées le 2026-11-08) :
- Prompt caching : https://platform.claude.com/docs/en/build-with-claude/prompt-caching
  input_tokens = tokens après le dernier point de cache ; total = input + cache_creation + cache_read.
- Web search : https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
  version simple web_search_20250305, max_uses par tour, usage.server_tool_use.web_search_requests.
- Batches : https://platform.claude.com/docs/en/build-with-claude/batch-processing
  processing_status "ended", résultats succeeded / errored / canceled / expired, conservés 29 jours.
- Effort : https://platform.claude.com/docs/en/build-with-claude/effort  (output_config.effort)
- Erreurs : https://platform.claude.com/docs/en/api/errors

Le SDK ne fait aucune reprise (max_retries=0) : l'adaptateur décide et journalise chaque tentative.
Aucune clé n'est lue ici : le SDK la prend dans l'environnement (ANTHROPIC_API_KEY), et tous les messages
d'erreur passent par redact().
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import anthropic

from journal.redact import redact
from llm.pricing import Usage
from llm.providers.base import BatchItemResult, ProviderError, ProviderRequest, ProviderResponse

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 120.0
WEB_SEARCH_TYPE = "web_search_20250305"            # version simple, sans filtrage dynamique
_WEB_SEARCH_KEYS = ("max_uses", "allowed_domains", "blocked_domains", "user_location")
_RETRYABLE_STATUS = {408, 429}                      # + tout 5xx (dont 529 surcharge)
MAX_ERROR_LEN = 300

AlertOnce = Callable[[str, str], None]


def _error(exc: BaseException, alert_once: AlertOnce) -> ProviderError:
    """Traduit une exception du SDK en ProviderError (retryable ou non). Message masqué et tronqué."""
    text = redact(f"anthropic {type(exc).__name__}: {exc}")[:MAX_ERROR_LEN]
    if isinstance(exc, anthropic.APIConnectionError):          # coupure réseau, y compris APITimeoutError
        return ProviderError(text, retryable=True)
    if isinstance(exc, anthropic.AuthenticationError):
        alert_once("anthropic:auth", "Anthropic : erreur d'authentification (401). Vérifier ANTHROPIC_API_KEY "
                                     "sur le serveur ; aucun appel ne passera tant qu'elle n'est pas corrigée.")
        return ProviderError(text, retryable=False)
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        return ProviderError(text, retryable=status in _RETRYABLE_STATUS or status >= 500)
    return ProviderError(text, retryable=False)


def _tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Outil autorisé par l'adaptateur -> définition Messages API. Rien n'est ajouté à la liste."""
    if tool.get("name") == "web_search" or str(tool.get("type", "")).startswith("web_search"):
        out = {"type": WEB_SEARCH_TYPE, "name": "web_search"}
        out.update({k: tool[k] for k in _WEB_SEARCH_KEYS if k in tool})
        return out
    return dict(tool)


def build_params(request: ProviderRequest) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": request.messages,
    }
    if request.system:
        # Point de cache sur la partie stable (cache à 5 minutes : pas de "ttl").
        params["system"] = [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}]
    output_config: dict[str, Any] = {}
    if request.effort:
        output_config["effort"] = request.effort
    if request.schema:
        output_config["format"] = {"type": "json_schema", "schema": request.schema}
    if output_config:
        params["output_config"] = output_config
    if request.tools:
        params["tools"] = [_tool(t) for t in request.tools]
    if request.temperature is not None:
        params["temperature"] = request.temperature
    return params


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def map_usage(usage: Any) -> Usage:
    return Usage(
        tokens_in=_get(usage, "input_tokens") or 0,
        tokens_out=_get(usage, "output_tokens") or 0,
        cache_read=_get(usage, "cache_read_input_tokens") or 0,
        cache_write=_get(usage, "cache_creation_input_tokens") or 0,
        web_search_requests=_get(_get(usage, "server_tool_use"), "web_search_requests") or 0,
    )


def map_message(message: Any) -> ProviderResponse:
    text = "".join(_get(b, "text") or "" for b in _get(message, "content") or [] if _get(b, "type") == "text")
    return ProviderResponse(text=text, usage=map_usage(_get(message, "usage")),
                            stop_reason=_get(message, "stop_reason"))


def _map_batch_item(item: Any) -> BatchItemResult:
    custom_id = _get(item, "custom_id")
    result = _get(item, "result")
    kind = _get(result, "type")
    if kind == "succeeded":
        return BatchItemResult(custom_id, map_message(_get(result, "message")))
    if kind == "errored":
        err = _get(_get(result, "error"), "error")
        detail = f"{_get(err, 'type', '?')}: {_get(err, 'message', '')}"
        return BatchItemResult(custom_id, None, redact(f"errored: {detail}")[:MAX_ERROR_LEN])
    return BatchItemResult(custom_id, None, kind or "unknown")     # canceled, expired : non facturés


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, options: dict[str, Any] | None = None, alert_once: AlertOnce | None = None,
                 client: Any = None):
        options = options or {}
        self.timeout_s = float(options.get("timeout_s", DEFAULT_TIMEOUT_S))
        self._alert_once = alert_once or (lambda key, text: log.warning("%s", text))
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:          # créé au premier appel réel seulement
            self._client = anthropic.Anthropic(max_retries=0, timeout=self.timeout_s)
        return self._client

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        try:
            message = self.client.messages.create(**build_params(request))
        except anthropic.AnthropicError as exc:
            raise _error(exc, self._alert_once) from None
        return map_message(message)

    def submit_batch(self, items: list[tuple[str, ProviderRequest]]) -> str:
        requests = [{"custom_id": cid, "params": build_params(req)} for cid, req in items]
        try:
            batch = self.client.messages.batches.create(requests=requests)
        except anthropic.AnthropicError as exc:
            raise _error(exc, self._alert_once) from None
        return batch.id

    def batch_results(self, batch_id: str) -> list[BatchItemResult] | None:
        try:
            batch = self.client.messages.batches.retrieve(batch_id)
            if _get(batch, "processing_status") != "ended":
                return None
            return [_map_batch_item(item) for item in self.client.messages.batches.results(batch_id)]
        except anthropic.AnthropicError as exc:
            raise _error(exc, self._alert_once) from None
