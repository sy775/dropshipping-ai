"""Étape 1 : chargement et validation de config/agents.yaml et config/prices.yaml."""
import datetime as dt
import shutil
from pathlib import Path

import pytest
import yaml

from llm.config import KNOWN_AGENTS, ConfigError, load_prices, load_settings

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture
def config_dir(tmp_path):
    """Copie modifiable de la config du dépôt."""
    dst = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, dst)
    return dst


def _edit_agents(config_dir, mutate):
    path = config_dir / "agents.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_repo_config_loads_all_agents():
    s = load_settings(REPO_CONFIG)
    assert set(s.agents) == set(KNOWN_AGENTS)
    for a in s.agents.values():
        assert a.max_tokens > 0
        assert a.mode in ("sync", "batch")
        assert a.provider == "anthropic"


def test_default_models_match_spec():
    s = load_settings(REPO_CONFIG)
    assert s.agents["sourcing"].model == "claude-haiku-5-5"
    assert s.agents["sav"].model == "claude-haiku-5-5"
    for name in ("veille", "boutique", "marketing", "contenu"):
        assert s.agents[name].model == "claude-sonnet-5-5"
    assert s.agents["veille"].mode == "batch"
    assert s.agents["contenu"].mode == "batch"


def test_budgets_match_validated_values():
    b = load_settings(REPO_CONFIG).budgets
    assert b.prod_global == 60
    assert b.prod_per_shop == 45
    assert b.eval_global == 5
    assert b.alert_ratio == 0.8


def test_shop_override(config_dir):
    _edit_agents(config_dir, lambda d: d["budgets"]["prod"]["shop_overrides"].update({"shop-x": 10}))
    b = load_settings(config_dir).budgets
    assert b.shop_cap("shop-x") == 10
    assert b.shop_cap("shop-y") == 45


def test_max_tokens_is_mandatory(config_dir):
    _edit_agents(config_dir, lambda d: d["agents"]["sav"].pop("max_tokens"))
    with pytest.raises(ConfigError, match="max_tokens"):
        load_settings(config_dir)


def test_unknown_agent_rejected(config_dir):
    _edit_agents(config_dir, lambda d: d["agents"].update({"pirate": dict(d["agents"]["sav"])}))
    with pytest.raises(ConfigError, match="pirate"):
        load_settings(config_dir)


def test_invalid_mode_rejected(config_dir):
    _edit_agents(config_dir, lambda d: d["agents"]["sav"].update({"mode": "stream"}))
    with pytest.raises(ConfigError, match="mode"):
        load_settings(config_dir)


def test_model_change_is_config_only(config_dir):
    _edit_agents(config_dir, lambda d: d["agents"]["sourcing"].update({"model": "claude-sonnet-5-5"}))
    assert load_settings(config_dir).agents["sourcing"].model == "claude-sonnet-5-5"


def test_prices_verified_and_tiered():
    p = load_prices(REPO_CONFIG)
    haiku = p.models["claude-haiku-5-5"]
    assert isinstance(haiku.verified_on, dt.date)
    assert len(haiku.tiers) == 2
    assert haiku.tiers[0].up_to_input_tokens == 100_000
    assert haiku.tiers[1].input == pytest.approx(haiku.tiers[0].input * 5)
    assert "claude-sonnet-5-5" in p.models
    for m in p.models.values():
        assert m.tiers[-1].up_to_input_tokens is None
        assert m.batch_multiplier == 0.5


def test_prices_missing_verified_on_rejected(config_dir):
    path = config_dir / "prices.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["models"]["claude-haiku-5-5"].pop("verified_on")
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="verified_on"):
        load_prices(config_dir)
