"""Chargement et validation de config/agents.yaml et config/prices.yaml.

Toute erreur de configuration lève ConfigError au chargement : mieux vaut un cron qui
échoue au démarrage qu'un agent qui tourne avec un plafond absent.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

KNOWN_AGENTS = ("veille", "sourcing", "boutique", "contenu", "marketing", "sav")
MODES = ("sync", "batch")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
WEB_SEARCH_TOOL = "web_search"
REPO_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    pass


def default_config_dir() -> Path:
    env = os.environ.get("DROPSHIP_CONFIG_DIR")
    return Path(env) if env else REPO_ROOT / "config"


# ---------------------------------------------------------------- agents.yaml

@dataclass(frozen=True)
class AgentConfig:
    name: str
    provider: str
    model: str
    mode: str
    max_tokens: int
    temperature: float | None
    allowed_tools: tuple[str, ...]
    effort: str | None = None              # None = défaut du modèle (medium sur Haiku 5.5, high sur Sonnet 5.5)
    web_search_max_uses: int | None = None # obligatoire si un outil web est autorisé : borne son coût


@dataclass(frozen=True)
class Budgets:
    prod_global: float
    prod_per_shop: float
    eval_global: float
    alert_ratio: float
    shop_overrides: dict[str, float] = field(default_factory=dict)

    def shop_cap(self, shop_id: str) -> float:
        return self.shop_overrides.get(shop_id, self.prod_per_shop)


@dataclass(frozen=True)
class Settings:
    agents: dict[str, AgentConfig]
    budgets: Budgets
    chars_per_token: float
    input_margin: float
    batch_stale_hours: float
    max_retries: int
    retry_base_delay_s: float
    pending_ttl_minutes: float = 15.0        # au-delà, une réservation `pending` est abandonnée
    search_tokens_estimate: int = 4000       # tokens d'entrée estimés par recherche web (résultats relus)
    batch_results_retention_days: float = 29.0   # conservation des résultats de lot chez le fournisseur
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)   # options par fournisseur

    def agent(self, name: str) -> AgentConfig:
        if name not in self.agents:
            raise ConfigError(f"agent inconnu : {name!r}")
        return self.agents[name]


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"fichier de configuration absent : {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} : contenu invalide")
    return data


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d or d[key] is None:
        raise ConfigError(f"{where} : champ obligatoire manquant '{key}'")
    return d[key]


def _positive_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{where} : nombre positif attendu, reçu {value!r}")
    return float(value)


def _parse_agent(name: str, raw: dict) -> AgentConfig:
    where = f"agents.{name}"
    if name not in KNOWN_AGENTS:
        raise ConfigError(f"{where} : agent inconnu {name!r} (attendus : {', '.join(KNOWN_AGENTS)})")
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} : objet attendu")
    mode = _require(raw, "mode", where)
    if mode not in MODES:
        raise ConfigError(f"{where} : mode invalide {mode!r} (attendus : sync, batch)")
    max_tokens = _require(raw, "max_tokens", where)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ConfigError(f"{where} : max_tokens doit être un entier > 0")
    temperature = raw.get("temperature")
    if temperature is not None:
        temperature = _positive_number(temperature, f"{where}.temperature")
    if "tools_allowed" in raw:
        # Ancien nom : le refuser plutôt que l'ignorer (sinon l'agent perdrait ses outils sans bruit).
        raise ConfigError(f"{where} : 'tools_allowed' a été renommé en 'allowed_tools'")
    tools = raw.get("allowed_tools") or []   # absente = aucun outil
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise ConfigError(f"{where} : allowed_tools doit être une liste de noms")
    effort = raw.get("effort")
    if effort is not None and effort not in EFFORTS:
        raise ConfigError(f"{where} : effort invalide {effort!r} (attendus : {', '.join(EFFORTS)}, ou null)")
    max_uses = raw.get("web_search_max_uses")
    if max_uses is not None and (isinstance(max_uses, bool) or not isinstance(max_uses, int) or max_uses < 1):
        raise ConfigError(f"{where} : web_search_max_uses doit être un entier >= 1")
    if max_uses is None:
        # Même détection que le garde-fou de l'adaptateur (web_search, web_fetch, ...). Import tardif :
        # llm.guards importe ce module.
        from llm.guards import is_web_tool
        web = [t for t in tools if is_web_tool({"name": t})]
        if web:
            raise ConfigError(f"{where} : outil web {web[0]!r} autorisé sans web_search_max_uses "
                              f"(coût non borné)")
    return AgentConfig(
        name=name,
        provider=str(_require(raw, "provider", where)),
        model=str(_require(raw, "model", where)),
        mode=mode,
        max_tokens=max_tokens,
        temperature=temperature,
        allowed_tools=tuple(tools),
        effort=effort,
        web_search_max_uses=max_uses,
    )


def load_settings(config_dir: Path | str | None = None) -> Settings:
    config_dir = Path(config_dir) if config_dir else default_config_dir()
    data = _read_yaml(config_dir / "agents.yaml")

    raw_agents = _require(data, "agents", "agents.yaml")
    agents = {name: _parse_agent(name, raw) for name, raw in raw_agents.items()}
    missing = set(KNOWN_AGENTS) - set(agents)
    if missing:
        raise ConfigError(f"agents.yaml : agents manquants {sorted(missing)}")

    b = _require(data, "budgets", "agents.yaml")
    prod = _require(b, "prod", "budgets")
    ev = _require(b, "eval", "budgets")
    overrides = prod.get("shop_overrides") or {}
    budgets = Budgets(
        prod_global=_positive_number(_require(prod, "global_monthly", "budgets.prod"), "budgets.prod.global_monthly"),
        prod_per_shop=_positive_number(_require(prod, "per_shop_monthly", "budgets.prod"), "budgets.prod.per_shop_monthly"),
        eval_global=_positive_number(_require(ev, "global_monthly", "budgets.eval"), "budgets.eval.global_monthly"),
        alert_ratio=_positive_number(_require(b, "alert_ratio", "budgets"), "budgets.alert_ratio"),
        shop_overrides={str(k): _positive_number(v, f"shop_overrides.{k}") for k, v in overrides.items()},
    )
    if not 0 < budgets.alert_ratio < 1:
        raise ConfigError("budgets.alert_ratio doit être entre 0 et 1")

    est = _require(data, "estimation", "agents.yaml")
    batch = _require(data, "batch", "agents.yaml")
    retry = _require(data, "retry", "agents.yaml")
    max_retries = _require(retry, "max_retries", "retry")
    if not isinstance(max_retries, int) or not 0 <= max_retries <= 2:
        raise ConfigError("retry.max_retries : entier entre 0 et 2 (règle 6)")
    if not est.get("chars_per_token"):
        raise ConfigError("estimation.chars_per_token doit être > 0")
    reservations = data.get("reservations") or {}          # section optionnelle, défaut 15 min
    ttl = _positive_number(reservations.get("pending_ttl_minutes", 15), "reservations.pending_ttl_minutes")
    if ttl <= 0:
        raise ConfigError("reservations.pending_ttl_minutes doit être > 0")
    search_tokens = _require(est, "search_tokens_estimate", "estimation")
    if isinstance(search_tokens, bool) or not isinstance(search_tokens, int) or search_tokens < 0:
        raise ConfigError("estimation.search_tokens_estimate : entier >= 0 attendu")
    providers = _parse_providers(data.get("providers") or {}, ttl)
    stale_hours = _positive_number(_require(batch, "stale_after_hours", "batch"), "stale_after_hours")
    retention = _positive_number(_require(batch, "results_retention_days", "batch"), "results_retention_days")
    if retention * 24 <= stale_hours:
        raise ConfigError("batch.results_retention_days doit dépasser batch.stale_after_hours")
    return Settings(
        agents=agents,
        budgets=budgets,
        chars_per_token=_positive_number(_require(est, "chars_per_token", "estimation"), "chars_per_token"),
        input_margin=_positive_number(_require(est, "input_margin", "estimation"), "input_margin"),
        batch_stale_hours=stale_hours,
        batch_results_retention_days=retention,
        max_retries=max_retries,
        retry_base_delay_s=_positive_number(_require(retry, "base_delay_s", "retry"), "base_delay_s"),
        pending_ttl_minutes=ttl,
        search_tokens_estimate=search_tokens,
        providers=providers,
    )


DEFAULT_PROVIDER_TIMEOUT_S = 120.0


def _parse_providers(raw: dict, pending_ttl_minutes: float) -> dict[str, dict[str, Any]]:
    """Options par fournisseur. timeout_s : délai maximal d'une tentative (défaut 120 s). Il doit rester
    strictement inférieur à la durée de vie d'une réservation `pending`, sinon une réservation encore en
    cours pourrait être libérée comme abandonnée pendant que l'appel est facturé."""
    if not isinstance(raw, dict):
        raise ConfigError("providers : objet attendu")
    out: dict[str, dict[str, Any]] = {}
    for name, opts in raw.items():
        opts = dict(opts or {})
        where = f"providers.{name}.timeout_s"
        timeout = _positive_number(opts.get("timeout_s", DEFAULT_PROVIDER_TIMEOUT_S), where)
        if timeout <= 0:
            raise ConfigError(f"{where} doit être > 0")
        if timeout >= pending_ttl_minutes * 60:
            raise ConfigError(f"{where} ({timeout:g} s) doit être < reservations.pending_ttl_minutes "
                              f"({pending_ttl_minutes:g} min = {pending_ttl_minutes * 60:g} s)")
        opts["timeout_s"] = timeout
        out[str(name)] = opts
    return out


# ---------------------------------------------------------------- prices.yaml

@dataclass(frozen=True)
class PriceTier:
    up_to_input_tokens: int | None
    input: float
    output: float
    cache_write: float
    cache_read: float


@dataclass(frozen=True)
class ModelPrice:
    model: str
    provider: str
    verified_on: dt.date
    valid_until: dt.date | None
    batch_multiplier: float
    tiers: tuple[PriceTier, ...]
    web_search_per_1000: float = 0.0     # frais de recherche web du fournisseur (server_tools dans prices.yaml)

    def tier_for(self, prompt_tokens: int) -> PriceTier:
        for tier in self.tiers:
            if tier.up_to_input_tokens is None or prompt_tokens <= tier.up_to_input_tokens:
                return tier
        return self.tiers[-1]


@dataclass(frozen=True)
class PriceTable:
    models: dict[str, ModelPrice]


def _as_date(value: Any, where: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{where} : date invalide {value!r}") from exc


def _parse_model_price(model: str, raw: dict, server_tools: dict) -> ModelPrice:
    where = f"prices.{model}"
    raw_tiers = _require(raw, "tiers", where)
    if not isinstance(raw_tiers, list) or not raw_tiers:
        raise ConfigError(f"{where} : au moins un palier attendu")
    tiers = []
    for i, t in enumerate(raw_tiers):
        tw = f"{where}.tiers[{i}]"
        up_to = t.get("up_to_input_tokens")
        tiers.append(PriceTier(
            up_to_input_tokens=None if up_to is None else int(up_to),
            input=_positive_number(_require(t, "input", tw), tw),
            output=_positive_number(_require(t, "output", tw), tw),
            cache_write=_positive_number(_require(t, "cache_write", tw), tw),
            cache_read=_positive_number(_require(t, "cache_read", tw), tw),
        ))
    if tiers[-1].up_to_input_tokens is not None:
        raise ConfigError(f"{where} : le dernier palier doit avoir up_to_input_tokens: null")
    bounds = [t.up_to_input_tokens for t in tiers[:-1]]
    if None in bounds or bounds != sorted(bounds):
        raise ConfigError(f"{where} : paliers non ordonnés")
    valid_until = raw.get("valid_until")
    provider = str(_require(raw, "provider", where))
    tools = server_tools.get(provider) or {}
    return ModelPrice(
        model=model,
        provider=provider,
        verified_on=_as_date(_require(raw, "verified_on", where), f"{where}.verified_on"),
        valid_until=None if valid_until is None else _as_date(valid_until, f"{where}.valid_until"),
        batch_multiplier=_positive_number(_require(raw, "batch_multiplier", where), f"{where}.batch_multiplier"),
        tiers=tuple(tiers),
        web_search_per_1000=_positive_number(tools.get("web_search_per_1000", 0),
                                             f"server_tools.{provider}.web_search_per_1000"),
    )


def load_prices(config_dir: Path | str | None = None) -> PriceTable:
    config_dir = Path(config_dir) if config_dir else default_config_dir()
    data = _read_yaml(config_dir / "prices.yaml")
    models = _require(data, "models", "prices.yaml")
    server_tools = data.get("server_tools") or {}
    return PriceTable(models={m: _parse_model_price(m, raw, server_tools) for m, raw in models.items()})
