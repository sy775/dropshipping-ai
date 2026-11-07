"""Calcul déterministe des coûts LLM depuis prices.yaml. Aucune décision ici ne passe par un LLM."""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from typing import Any

from llm.config import ModelPrice, PriceTable

PER_MILLION = 1_000_000


@dataclass(frozen=True)
class Usage:
    tokens_in: int = 0          # entrée hors cache
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def prompt_tokens(self) -> int:
        """Taille du prompt au sens tarifaire : entrée + lectures + écritures de cache."""
        return self.tokens_in + self.cache_read + self.cache_write


def compute_cost(price: ModelPrice, usage: Usage, batch: bool = False) -> float:
    tier = price.tier_for(usage.prompt_tokens)
    cost = (usage.tokens_in * tier.input
            + usage.tokens_out * tier.output
            + usage.cache_write * tier.cache_write
            + usage.cache_read * tier.cache_read) / PER_MILLION
    return cost * price.batch_multiplier if batch else cost


# ---------------------------------------------------------------- tarif absent / expiré

@dataclass(frozen=True)
class PriceLookup:
    price: ModelPrice
    issue: str | None          # None, "missing" ou "expired"


def resolve_price(table: PriceTable, model: str, today: dt.date) -> PriceLookup:
    """Renvoie toujours un tarif utilisable (règle 5 : l'appel continue), et signale le problème.

    - expiré (valid_until dépassé) : on garde ce tarif, c'est le dernier connu ;
    - absent : aucun dernier tarif connu pour ce modèle, on prend le plus cher de la table,
      pour que les plafonds restent prudents.
    """
    price = table.models.get(model)
    if price is None:
        fallback = max(table.models.values(), key=lambda p: (p.tiers[-1].output, p.tiers[-1].input))
        return PriceLookup(fallback, "missing")
    if price.valid_until is not None and today > price.valid_until:
        return PriceLookup(price, "expired")
    return PriceLookup(price, None)


# ---------------------------------------------------------------- estimation avant appel

def estimate_raw_tokens(chars: int, chars_per_token: float) -> int:
    return math.ceil(chars / chars_per_token)


def payload_chars(*parts: Any) -> int:
    """Taille en caractères de tout ce qui part au modèle (messages, préfixe, schéma, outils)."""
    total = 0
    for part in parts:
        if part is None:
            continue
        total += len(part) if isinstance(part, str) else len(json.dumps(part, ensure_ascii=False, default=str))
    return total


@dataclass(frozen=True)
class Estimate:
    input_tokens: int        # entrée majorée de la marge
    output_tokens: int       # max_tokens (pire cas)
    upper_tier: bool         # au-delà du premier palier tarifaire
    cost_usd: float


def estimate_cost(price: ModelPrice, raw_input_tokens: int, max_tokens: int, margin: float,
                  batch: bool = False, cached_prefix_tokens: int = 0) -> Estimate:
    """Coût maximal estimé. Le palier se décide sur l'entrée majorée. Le préfixe à mettre en cache
    est compté au tarif d'écriture en cache (pire cas : le cache est froid)."""
    input_est = math.ceil(raw_input_tokens * (1 + margin))
    prefix_est = min(math.ceil(cached_prefix_tokens * (1 + margin)), input_est)
    tier = price.tier_for(input_est)
    cost = ((input_est - prefix_est) * tier.input
            + prefix_est * tier.cache_write
            + max_tokens * tier.output) / PER_MILLION
    if batch:
        cost *= price.batch_multiplier
    return Estimate(
        input_tokens=input_est,
        output_tokens=max_tokens,
        upper_tier=tier is not price.tiers[0],
        cost_usd=cost,
    )
