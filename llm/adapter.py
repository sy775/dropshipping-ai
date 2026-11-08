"""Point d'entrée unique des appels LLM pour tous les agents.

    from llm.adapter import call
    res = call("sourcing", shop_id, messages, schema=...)

En mode `batch`, `call` renvoie un BatchHandle ; le cron appelle ensuite `collect_batch` et
`check_stale_batches`. Tous les garde-fous sont appliqués ici, avant tout envoi au fournisseur.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jsonschema

from journal import db
from journal.redact import redact
from llm import guards
from llm.config import AgentConfig, ModelPrice
from llm.guards import BudgetExceeded, Cap, GuardError, notify
from llm.pricing import Usage, compute_cost, estimate_cost, estimate_raw_tokens, payload_chars, resolve_price
from llm.providers.base import ProviderError, ProviderRequest, ProviderResponse
from llm.runtime import Runtime, get_runtime

log = logging.getLogger(__name__)

PURPOSES = ("prod", "eval")
LONG_PROMPT_TOKENS = 100_000      # règle 4
MAX_ERROR_LEN = 500
PROVISIONAL_BATCH_PREFIX = "pending-"   # batch_id provisoire tant que l'identifiant fournisseur n'est pas écrit

# Motifs d'arrêt (vocabulaire Messages API). Seuls ceux-ci sont un succès ; refusal est un refus ; tout autre
# motif (max_tokens, model_context_window_exceeded, pause_turn, tool_use, inconnu ou absent) est une réponse
# incomplète. Dans tous les cas d'échec, le coût est journalisé et aucun contenu n'est renvoyé.
SUCCESS_STOPS = frozenset({"end_turn", "stop_sequence"})
REFUSAL_STOP = "refusal"


class LLMCallError(Exception):
    """Échec définitif après les nouvelles tentatives autorisées."""


class LLMIncompleteError(Exception):
    """Réponse tronquée ou interrompue. Coût journalisé, pas de nouvelle tentative automatique
    (elle coûterait autant), aucun contenu renvoyé."""

    def __init__(self, message: str, call_id: str, stop_reason: str | None):
        super().__init__(message)
        self.call_id, self.stop_reason = call_id, stop_reason


class LLMRefusedError(Exception):
    """Le modèle a refusé de répondre (stop_reason = refusal). Coût journalisé, pas de nouvelle tentative,
    aucun contenu renvoyé, alerte Telegram une fois par jour et par agent."""

    def __init__(self, message: str, call_id: str):
        super().__init__(message)
        self.call_id = call_id


class LLMFormatError(Exception):
    """La réponse ne respecte pas le schéma demandé. Le coût est quand même journalisé."""

    def __init__(self, message: str, call_id: str):
        super().__init__(message)
        self.call_id = call_id


@dataclass(frozen=True)
class LLMResult:
    content: Any                 # str, ou objet validé si un schéma est fourni
    usage: Usage
    cost_usd: float
    call_id: str
    model: str
    provider: str
    dry_run: bool


@dataclass(frozen=True)
class BatchHandle:
    batch_id: str
    call_id: str                 # ligne de réservation dans llm_calls
    reserved_usd: float
    n_items: int
    custom_ids: tuple[str, ...]
    dry_run: bool


@dataclass(frozen=True)
class BatchItemOutcome:
    custom_id: str
    content: Any
    usage: Usage | None
    cost_usd: float
    call_id: str
    error: str | None


# ====================================================================== entrée

def call(agent: str, shop_id: str | None, messages: list, schema: dict | None = None,
         mode: str | None = None, cache_prefix: str | None = None, tools: list[dict] | None = None,
         purpose: str = "prod", *, runtime: Runtime | None = None) -> LLMResult | BatchHandle:
    rt = runtime or get_runtime()
    cfg = rt.settings.agent(agent)
    mode = mode or cfg.mode
    if mode not in ("sync", "batch"):
        raise ValueError(f"mode invalide : {mode!r}")
    if purpose not in PURPOSES:
        raise ValueError(f"purpose invalide : {purpose!r}")
    ctx = _Ctx(rt, cfg, shop_id, mode, purpose)

    try:
        guards.check_tools(cfg, tools)
        tools = _prepare_tools(ctx, tools)
    except GuardError as exc:
        ctx.log_refusal(str(exc))
        raise

    if mode == "sync":
        return _call_sync(ctx, messages, schema, cache_prefix, tools)
    return _submit_batch(ctx, messages, schema, cache_prefix, tools)


# ====================================================================== contexte d'un appel

class _Ctx:
    def __init__(self, rt: Runtime, cfg: AgentConfig, shop_id: str | None, mode: str, purpose: str):
        self.rt, self.cfg, self.shop_id, self.mode, self.purpose = rt, cfg, shop_id, mode, purpose
        self.dry_run = not rt.live
        self.caps: list[Cap] = guards.caps_for(rt.settings.budgets, purpose, shop_id)

    @property
    def now(self) -> dt.datetime:
        return self.rt.clock()

    @property
    def month(self) -> str:
        return db.month_key(self.now)

    def row(self, **fields) -> dict:
        base = dict(ts=self.now, shop_id=self.shop_id, agent=self.cfg.name, provider=self.cfg.provider,
                    model=self.cfg.model, mode=self.mode, purpose=self.purpose, dry_run=int(self.dry_run))
        base.update(fields)
        return base

    def price(self) -> ModelPrice:
        lookup = resolve_price(self.rt.prices, self.cfg.model, self.now.date())
        if lookup.issue:
            label = "absent" if lookup.issue == "missing" else "expiré"
            key = f"price:{lookup.issue}:{self.cfg.model}:{self.now.date().isoformat()}"
            if db.record_alert_once(self.rt.conn, key, self.now):
                notify(self.rt.notifier,
                       f"Tarif {label} dans prices.yaml pour {self.cfg.model} : appel maintenu "
                       f"avec le tarif {lookup.price.model} (vérifié le {lookup.price.verified_on}).")
        return lookup.price

    def log_refusal(self, reason: str, **fields) -> None:
        db.insert_call(self.rt.conn, **self.row(status="refused", cost_usd=0, error=redact(reason), **fields))

    def refuse_budget(self, exc: BudgetExceeded, **fields) -> None:
        self.log_refusal(str(exc), **fields)
        key = f"refused:{exc.cap.key}:{self.now.date().isoformat()}"
        if db.record_alert_once(self.rt.conn, key, self.now):
            notify(self.rt.notifier, f"Appel LLM refusé ({self.cfg.name}, mode {self.mode}) : {exc}.")

    def after_spend(self, month: str | None = None) -> None:
        guards.threshold_alerts(self.rt.conn, self.caps, month or self.month,
                                self.rt.settings.budgets.alert_ratio, self.rt.notifier, self.now)

    def expire_orphans(self) -> list:
        """Libère les réservations `pending` expirées. À appeler en tête de la transaction de réservation."""
        expired = db.expire_pending(self.rt.conn, self.now, self.rt.settings.pending_ttl_minutes)
        for r in expired:
            log.warning("réservation pending abandonnée : call_id=%s agent=%s ts=%s %.4f $ libérés",
                        r["call_id"], r["agent"], r["ts"], r["cost_usd"])
        return expired

    def alert_orphans(self, expired: list) -> None:
        """Alerte (hors transaction, une fois par jour) si des réservations ont été abandonnées."""
        if not expired:
            return
        if db.record_alert_once(self.rt.conn, f"pending_abandoned:{self.now.date().isoformat()}", self.now):
            total = sum(r["cost_usd"] for r in expired)
            notify(self.rt.notifier,
                   f"{len(expired)} réservation(s) LLM 'pending' abandonnée(s) "
                   f"(> {self.rt.settings.pending_ttl_minutes:g} min, processus interrompu ?) : "
                   f"{total:.4f} $ libérés. Voir llm_calls status='abandoned'.")

    def alert_refusal(self, call_id: str) -> None:
        key = f"refusal:{self.cfg.name}:{self.now.date().isoformat()}"
        if db.record_alert_once(self.rt.conn, key, self.now):
            notify(self.rt.notifier, f"Refus du modèle ({self.cfg.name}, {self.cfg.model}, call_id {call_id}) : "
                                     f"stop_reason=refusal. Coût journalisé, aucune nouvelle tentative.")

    def check_long_prompt(self, usage: Usage, call_id: str) -> None:
        if usage.prompt_tokens > LONG_PROMPT_TOKENS:
            notify(self.rt.notifier,
                   f"Prompt de {usage.prompt_tokens} tokens (> 100 000) : agent {self.cfg.name}, "
                   f"modèle {self.cfg.model}, call_id {call_id}. Haiku 5.5 facture x5 au-delà.")


def _prepare_tools(ctx: _Ctx, tools: list[dict] | None) -> list[dict] | None:
    """Impose `max_uses` (agents.yaml) à chaque outil web, quelle que soit la valeur demandée : c'est cette
    borne qui rend le coût de recherche réservable. Refus si l'agent n'en a pas."""
    if not tools:
        return tools
    out = []
    for tool in tools:
        if guards.is_web_tool(tool):
            if ctx.cfg.web_search_max_uses is None:
                raise guards.ToolNotAllowed(
                    f"outil web pour {ctx.cfg.name} sans web_search_max_uses dans agents.yaml")
            tool = {**tool, "max_uses": ctx.cfg.web_search_max_uses}
        out.append(tool)
    return out


def _max_searches(ctx: _Ctx, tools: list[dict] | None) -> int:
    if any(guards.is_web_tool(t) for t in tools or []):
        return ctx.cfg.web_search_max_uses or 0
    return 0


def _estimate(ctx: _Ctx, price: ModelPrice, messages, schema, cache_prefix, tools, batch: bool = False):
    raw, prefix = _estimate_input(ctx, messages, schema, cache_prefix, tools)
    return estimate_cost(price, raw, ctx.cfg.max_tokens, ctx.rt.settings.input_margin, batch=batch,
                         cached_prefix_tokens=prefix, web_searches=_max_searches(ctx, tools),
                         search_tokens_each=ctx.rt.settings.search_tokens_estimate)


def _classify(stop_reason: str | None) -> str:
    """Statut du journal selon le motif d'arrêt : ok, refused ou incomplete."""
    if stop_reason in SUCCESS_STOPS:
        return "ok"
    return "refused" if stop_reason == REFUSAL_STOP else "incomplete"


def _estimate_input(ctx: _Ctx, messages, schema, cache_prefix, tools) -> tuple[int, int]:
    cpt = ctx.rt.settings.chars_per_token
    raw = estimate_raw_tokens(payload_chars(messages, cache_prefix, schema, tools), cpt)
    prefix = estimate_raw_tokens(len(cache_prefix), cpt) if cache_prefix else 0
    return raw, prefix


def _request(ctx: _Ctx, messages, schema, cache_prefix, tools) -> ProviderRequest:
    return ProviderRequest(model=ctx.cfg.model, messages=messages, max_tokens=ctx.cfg.max_tokens,
                           system=cache_prefix, schema=schema, tools=tools, temperature=ctx.cfg.temperature,
                           effort=ctx.cfg.effort)


def _parse(text: str, schema: dict | None) -> tuple[Any, str | None]:
    """Renvoie (contenu, erreur de format éventuelle)."""
    if schema is None:
        return text, None
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`").removeprefix("json").strip()
    try:
        obj = json.loads(body)
        jsonschema.validate(obj, schema)
    except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
        return None, f"sortie non conforme au schéma : {str(exc).splitlines()[0]}"
    return obj, None


def _outcome(resp: ProviderResponse, schema: dict | None) -> tuple[str, Any, str | None]:
    """(statut, contenu, erreur). Le contenu n'est renvoyé que pour un succès conforme au schéma."""
    status = _classify(resp.stop_reason)
    if status != "ok":
        return status, None, f"{status}: stop_reason={resp.stop_reason}"
    content, fmt_error = _parse(resp.text, schema)
    if fmt_error:
        return "format_error", None, fmt_error
    return "ok", content, None


def _error_text(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_LEN]


# ====================================================================== sync

def _call_sync(ctx: _Ctx, messages, schema, cache_prefix, tools) -> LLMResult:
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        raise ValueError("mode sync : messages doit être une liste non vide de messages")
    rt = ctx.rt
    price = ctx.price()
    est = _estimate(ctx, price, messages, schema, cache_prefix, tools)
    request = _request(ctx, messages, schema, cache_prefix, tools)
    provider = rt.provider_for(ctx.cfg.provider)
    estimates = dict(tokens_in_estimated=est.input_tokens, tokens_out_estimated=est.output_tokens,
                     cost_estimated_usd=est.cost_usd)

    max_attempts = 1 + rt.settings.max_retries
    for attempt in range(1, max_attempts + 1):
        # Contrôle du plafond + réservation du coût estimé, atomiquement (ligne "pending").
        refused = None
        with db.transaction(rt.conn):
            expired = ctx.expire_orphans()
            try:
                guards.check_budget(rt.conn, ctx.caps, ctx.month, est.cost_usd)
            except BudgetExceeded as exc:
                refused = exc
            else:
                call_id = db.insert_call(rt.conn, **ctx.row(status="pending", cost_usd=est.cost_usd,
                                                            attempt=attempt, **estimates))
        ctx.alert_orphans(expired)
        if refused:
            ctx.refuse_budget(refused, attempt=attempt, **estimates)
            raise refused

        t0 = time.monotonic()
        try:
            resp: ProviderResponse = provider.complete(request)
        except Exception as exc:  # noqa: BLE001 - toute erreur est journalisée puis relevée
            latency = int((time.monotonic() - t0) * 1000)
            db.update_call(rt.conn, call_id, status="error", cost_usd=0, latency_ms=latency,
                           error=_error_text(exc))
            retryable = isinstance(exc, ProviderError) and exc.retryable
            if retryable and attempt < max_attempts:
                rt.sleep(rt.settings.retry_base_delay_s * 2 ** (attempt - 1))
                continue
            raise LLMCallError(f"échec de l'appel {ctx.cfg.name} après {attempt} tentative(s) : "
                               f"{_error_text(exc)}") from None
        break

    latency = int((time.monotonic() - t0) * 1000)
    u = resp.usage
    cost = compute_cost(price, u)
    status, content, error = _outcome(resp, schema)
    db.update_call(rt.conn, call_id, status=status, cost_usd=cost, latency_ms=latency, tokens_in=u.tokens_in,
                   tokens_out=u.tokens_out, tokens_cache_read=u.cache_read, tokens_cache_write=u.cache_write,
                   web_search_requests=u.web_search_requests, stop_reason=resp.stop_reason,
                   error=redact(error) if error else None)
    ctx.after_spend()
    ctx.check_long_prompt(u, call_id)
    if status == "refused":
        ctx.alert_refusal(call_id)
        raise LLMRefusedError(f"refus du modèle pour {ctx.cfg.name} (call_id {call_id})", call_id)
    if status == "incomplete":
        raise LLMIncompleteError(f"réponse incomplète pour {ctx.cfg.name} : stop_reason={resp.stop_reason} "
                                 f"(call_id {call_id})", call_id, resp.stop_reason)
    if status == "format_error":
        raise LLMFormatError(error, call_id)
    return LLMResult(content=content, usage=u, cost_usd=cost, call_id=call_id, model=ctx.cfg.model,
                     provider=ctx.cfg.provider, dry_run=ctx.dry_run)


# ====================================================================== batch

def _submit_batch(ctx: _Ctx, conversations, schema, cache_prefix, tools) -> BatchHandle:
    if (not isinstance(conversations, list) or not conversations
            or not all(isinstance(c, list) and c and all(isinstance(m, dict) for m in c) for c in conversations)):
        raise ValueError("mode batch : messages doit être une liste non vide de conversations "
                         "(une liste de messages par élément du lot)")
    rt = ctx.rt
    price = ctx.price()
    items, total, tokens_in_est = [], 0.0, 0
    for i, conv in enumerate(conversations):
        est = _estimate(ctx, price, conv, schema, cache_prefix, tools, batch=True)
        total += est.cost_usd
        tokens_in_est += est.input_tokens
        items.append((f"item-{i}", _request(ctx, conv, schema, cache_prefix, tools)))
    estimates = dict(tokens_in_estimated=tokens_in_est, tokens_out_estimated=ctx.cfg.max_tokens * len(items),
                     cost_estimated_usd=total)

    # Règle 2 : réservation du coût maximal avant l'envoi, atomique avec le contrôle.
    refused = None
    with db.transaction(rt.conn):
        expired = ctx.expire_orphans()
        try:
            guards.check_budget(rt.conn, ctx.caps, ctx.month, total)
        except BudgetExceeded as exc:
            refused = exc
        else:
            # Identifiant provisoire écrit avec la réservation : si le processus meurt entre l'envoi et
            # l'écriture de l'identifiant fournisseur, check_stale_batches retrouve quand même la ligne.
            call_id = uuid.uuid4().hex
            db.insert_call(rt.conn, **ctx.row(call_id=call_id, status="reserved", cost_usd=total,
                                              batch_id=f"{PROVISIONAL_BATCH_PREFIX}{call_id}", **estimates))
    ctx.alert_orphans(expired)
    if refused:
        ctx.refuse_budget(refused, **estimates)
        raise refused

    provider = rt.provider_for(ctx.cfg.provider)
    max_attempts = 1 + rt.settings.max_retries
    for attempt in range(1, max_attempts + 1):
        try:
            batch_id = provider.submit_batch(items)
            break
        except Exception as exc:  # noqa: BLE001
            db.insert_call(rt.conn, **ctx.row(status="error", cost_usd=0, attempt=attempt, error=_error_text(exc)))
            retryable = isinstance(exc, ProviderError) and exc.retryable
            if retryable and attempt < max_attempts:
                rt.sleep(rt.settings.retry_base_delay_s * 2 ** (attempt - 1))
                continue
            db.update_call(rt.conn, call_id, status="error", cost_usd=0, error="lot non envoyé")
            raise LLMCallError(f"échec de l'envoi du lot {ctx.cfg.name} : {_error_text(exc)}") from None

    db.update_call(rt.conn, call_id, batch_id=batch_id, attempt=attempt)
    ctx.after_spend()
    return BatchHandle(batch_id=batch_id, call_id=call_id, reserved_usd=total, n_items=len(items),
                       custom_ids=tuple(cid for cid, _ in items), dry_run=ctx.dry_run)


def collect_batch(batch_id: str, schema: dict | None = None, *,
                  runtime: Runtime | None = None) -> dict[str, BatchItemOutcome] | None:
    """Récupère un lot terminé : une ligne par élément au coût réel (tarif batch), et la réservation
    est libérée. Renvoie None si le lot n'est pas encore terminé."""
    rt = runtime or get_runtime()
    res_row = rt.conn.execute("SELECT * FROM llm_calls WHERE batch_id = ? AND status = 'reserved'",
                              (batch_id,)).fetchone()
    if res_row is None:
        raise LookupError(f"lot inconnu ou déjà collecté : {batch_id}")
    cfg = rt.settings.agent(res_row["agent"])
    ctx = _Ctx(rt, cfg, res_row["shop_id"], "batch", res_row["purpose"])
    ctx.dry_run = bool(res_row["dry_run"])
    provider = rt.provider_for(res_row["provider"], live=not ctx.dry_run)

    results = provider.batch_results(batch_id)
    if results is None:
        return None

    lookup = resolve_price(rt.prices, res_row["model"], ctx.now.date())
    billing_month = res_row["billing_month"] or res_row["ts"][:7]
    outcomes: dict[str, BatchItemOutcome] = {}
    refused_ids: list[str] = []
    with db.transaction(rt.conn):
        for r in results:
            # Lignes réelles imputées au mois d'envoi (celui de la réservation), pas au mois de collecte.
            common = ctx.row(model=res_row["model"], provider=res_row["provider"], batch_id=batch_id,
                             billing_month=billing_month)
            if r.response is None:
                cid = db.insert_call(rt.conn, **common, status="error", cost_usd=0, error=redact(r.error or "?"))
                outcomes[r.custom_id] = BatchItemOutcome(r.custom_id, None, None, 0.0, cid, redact(r.error or "?"))
                continue
            u = r.response.usage
            cost = compute_cost(lookup.price, u, batch=True)
            status, content, error = _outcome(r.response, schema)
            cid = db.insert_call(rt.conn, **common, status=status, cost_usd=cost, tokens_in=u.tokens_in,
                                 tokens_out=u.tokens_out, tokens_cache_read=u.cache_read,
                                 tokens_cache_write=u.cache_write, web_search_requests=u.web_search_requests,
                                 stop_reason=r.response.stop_reason, error=redact(error) if error else None)
            if status == "refused":
                refused_ids.append(cid)
            outcomes[r.custom_id] = BatchItemOutcome(r.custom_id, content, u, cost, cid, error)
        # La réservation est remplacée par les coûts réels ci-dessus.
        db.update_call(rt.conn, res_row["call_id"], status="released", cost_usd=0)

    ctx.after_spend(billing_month)
    if refused_ids:
        ctx.alert_refusal(refused_ids[0])
    for o in outcomes.values():
        if o.usage:
            ctx.check_long_prompt(o.usage, o.call_id)
    return outcomes


def check_stale_batches(*, runtime: Runtime | None = None) -> list[str]:
    """Lots réservés et non collectés depuis plus de `stale_after_hours` : alerte une fois par lot.

    Couvre aussi les réservations dont l'identifiant est resté provisoire (`pending-<call_id>`) :
    lot peut-être envoyé et facturé, mais identifiant fournisseur perdu. La réservation reste comptée
    dans le cumul jusqu'à vérification manuelle chez le fournisseur."""
    rt = runtime or get_runtime()
    now = rt.clock()
    limit = now - dt.timedelta(hours=rt.settings.batch_stale_hours)
    stale = []
    for r in rt.conn.execute("SELECT call_id, batch_id, ts, agent, cost_usd FROM llm_calls "
                             "WHERE status = 'reserved' ORDER BY ts"):
        if dt.datetime.fromisoformat(r["ts"]) > limit:
            continue
        batch_id = r["batch_id"] or f"{PROVISIONAL_BATCH_PREFIX}{r['call_id']}"   # lignes antérieures au correctif
        stale.append(batch_id)
        if not db.record_alert_once(rt.conn, f"batch_stale:{batch_id}", now):
            continue
        if batch_id.startswith(PROVISIONAL_BATCH_PREFIX):
            notify(rt.notifier, f"Lot {r['agent']} réservé il y a plus de {rt.settings.batch_stale_hours:g} h sans "
                                f"identifiant fournisseur (call_id {r['call_id']}) : envoi interrompu ? Vérifier "
                                f"chez le fournisseur ; {r['cost_usd']:.4f} $ restent réservés.")
        else:
            notify(rt.notifier, f"Lot {batch_id} ({r['agent']}) non collecté depuis plus de "
                                f"{rt.settings.batch_stale_hours:g} h ; {r['cost_usd']:.4f} $ restent réservés.")
    return stale
