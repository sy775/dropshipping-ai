"""Étape 4 : calcul du coût depuis prices.yaml, paliers, tarif absent ou expiré, estimation avant appel."""
import dataclasses
import datetime as dt
from pathlib import Path

import pytest

from llm.config import load_prices
from llm.pricing import Usage, compute_cost, estimate_cost, estimate_raw_tokens, resolve_price

PRICES = load_prices(Path(__file__).resolve().parents[1] / "config")
HAIKU = PRICES.models["claude-haiku-5-5"]
SONNET = PRICES.models["claude-sonnet-5-5"]
TODAY = dt.date(2026, 11, 8)


# --- coût réel : valeurs calculées à la main depuis la page pricing (USD / MTok) ---

def test_cost_haiku_standard():
    # 1 000 x 0,10 + 500 x 0,50 = 100 + 250 = 350 / 1e6
    assert compute_cost(HAIKU, Usage(tokens_in=1000, tokens_out=500)) == pytest.approx(0.00035)


def test_cost_sonnet_with_cache():
    # 200 x 2 + 10 000 x 0,10 (lecture) + 5 000 x 2,50 (écriture) + 300 x 10
    # = 400 + 1 000 + 12 500 + 3 000 = 16 900 / 1e6
    u = Usage(tokens_in=200, tokens_out=300, cache_read=10_000, cache_write=5_000)
    assert compute_cost(SONNET, u) == pytest.approx(0.0169)


def test_cost_haiku_long_prompt_tier():
    # 150 000 > 100 000 : palier x5. 150 000 x 0,50 + 1 000 x 2,50 = 75 000 + 2 500 = 77 500 / 1e6
    assert compute_cost(HAIKU, Usage(tokens_in=150_000, tokens_out=1000)) == pytest.approx(0.0775)


def test_tier_counts_cache_tokens():
    # 50 000 entrée + 60 000 lus en cache = 110 000 de prompt : palier supérieur pour toute la requête
    u = Usage(tokens_in=50_000, tokens_out=0, cache_read=60_000)
    assert compute_cost(HAIKU, u) == pytest.approx((50_000 * 0.50 + 60_000 * 0.05) / 1e6)


def test_cost_batch_discount():
    # (1 000 x 2 + 1 000 x 10) x 0,5 = 6 000 / 1e6
    assert compute_cost(SONNET, Usage(tokens_in=1000, tokens_out=1000), batch=True) == pytest.approx(0.006)


# --- tarif absent ou expiré : signalé, mais un tarif est toujours renvoyé ---

def test_price_ok():
    r = resolve_price(PRICES, "claude-haiku-5-5", TODAY)
    assert r.issue is None and r.price is HAIKU


def test_price_expired_keeps_last_known():
    expired = dataclasses.replace(HAIKU, valid_until=TODAY - dt.timedelta(days=1))
    table = dataclasses.replace(PRICES, models={**PRICES.models, "claude-haiku-5-5": expired})
    r = resolve_price(table, "claude-haiku-5-5", TODAY)
    assert r.issue == "expired"
    assert r.price is expired


def test_price_valid_until_today_is_not_expired():
    promo = dataclasses.replace(HAIKU, valid_until=TODAY)
    table = dataclasses.replace(PRICES, models={**PRICES.models, "claude-haiku-5-5": promo})
    assert resolve_price(table, "claude-haiku-5-5", TODAY).issue is None


def test_price_missing_falls_back_to_most_expensive():
    r = resolve_price(PRICES, "modele-inconnu", TODAY)
    assert r.issue == "missing"
    assert r.price.model == "claude-opus-5-5"  # repli prudent pour les plafonds


# --- estimation avant appel ---

def test_estimate_raw_tokens_from_chars():
    assert estimate_raw_tokens(350, chars_per_token=3.5) == 100
    assert estimate_raw_tokens(351, chars_per_token=3.5) == 101  # arrondi supérieur


def test_estimate_applies_margin_and_max_tokens():
    e = estimate_cost(SONNET, raw_input_tokens=1000, max_tokens=2000, margin=0.20)
    assert e.input_tokens == 1200
    assert e.output_tokens == 2000
    assert e.cost_usd == pytest.approx((1200 * 2 + 2000 * 10) / 1e6)


def test_estimate_90k_goes_to_upper_tier_because_of_margin():
    # 90 000 estimés -> 108 000 avec +20 % : palier > 100 000 de Haiku
    e = estimate_cost(HAIKU, raw_input_tokens=90_000, max_tokens=1000, margin=0.20)
    assert e.input_tokens == 108_000
    assert e.upper_tier is True
    assert e.cost_usd == pytest.approx((108_000 * 0.50 + 1000 * 2.50) / 1e6)


def test_estimate_cached_prefix_priced_as_cache_write():
    # pire cas : le préfixe est écrit en cache (1,25 x l'entrée)
    e = estimate_cost(SONNET, raw_input_tokens=1000, max_tokens=0, margin=0.0, cached_prefix_tokens=800)
    assert e.cost_usd == pytest.approx((200 * 2 + 800 * 2.5) / 1e6)


def test_estimate_batch():
    e = estimate_cost(SONNET, raw_input_tokens=1000, max_tokens=1000, margin=0.0, batch=True)
    assert e.cost_usd == pytest.approx(0.006)
