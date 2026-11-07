"""Étape 5 : adaptateur en mode sync et garde-fous (plafonds, outils, tarifs, tentatives, dry-run)."""
import dataclasses
import datetime as dt

import pytest

from journal import db
from llm import adapter
from llm.guards import BudgetExceeded, ToolNotAllowed
from llm.pricing import Usage
from llm.providers.base import ProviderError, ProviderResponse
from llm.providers.fake import FakeProvider
from tests.conftest import edit_yaml, rows, seed_spend
from tests.fakes import ANTHROPIC_KEY_PREFIX, fake_anthropic_key

MSG = [{"role": "user", "content": "Bonjour"}]
WEB_TOOL = {"type": "web_search_20260209", "name": "web_search"}


def fixed(usage: Usage, text: str = "ok"):
    return FakeProvider(responder=lambda req: ProviderResponse(text=text, usage=usage))


# --- critère : coût journalisé = calcul manuel (3 cas de tarifs, dont un en cache) ---

@pytest.mark.parametrize("agent, usage, expected", [
    ("sourcing", Usage(tokens_in=1000, tokens_out=500), 0.00035),                     # Haiku
    ("marketing", Usage(tokens_in=200, tokens_out=300, cache_read=10_000, cache_write=5_000), 0.0169),  # Sonnet + cache
    ("sav", Usage(tokens_in=150_000, tokens_out=1000), 0.0775),                       # Haiku > 100 000
])
def test_logged_cost_matches_manual_calculation(make_runtime, agent, usage, expected):
    rt = make_runtime(provider=fixed(usage))
    res = adapter.call(agent, "shop-a", MSG, runtime=rt)
    assert res.cost_usd == pytest.approx(expected)
    (row,) = rows(rt)
    assert row["status"] == "ok"
    assert row["cost_usd"] == pytest.approx(expected)
    assert (row["tokens_in"], row["tokens_out"], row["tokens_cache_read"], row["tokens_cache_write"]) == (
        usage.tokens_in, usage.tokens_out, usage.cache_read, usage.cache_write)
    assert row["call_id"] == res.call_id
    assert row["agent"] == agent and row["shop_id"] == "shop-a" and row["purpose"] == "prod"


def test_estimated_and_real_tokens_logged(rt):
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    (row,) = rows(rt)
    assert row["tokens_in_estimated"] > 0
    assert row["tokens_out_estimated"] == 2048          # max_tokens de sourcing
    assert row["cost_estimated_usd"] > 0
    assert row["tokens_in"] is not None


def test_request_uses_agent_config(rt):
    adapter.call("sourcing", None, MSG, cache_prefix="Playbook stable", runtime=rt)
    (req,) = rt.fake.requests
    assert req.model == "claude-haiku-5-5"
    assert req.max_tokens == 2048
    assert req.system == "Playbook stable"


# --- critère : changer le modèle dans agents.yaml, sans toucher au code ---

def test_model_switch_is_config_only(config_dir, make_runtime):
    edit_yaml(config_dir / "agents.yaml", lambda d: d["agents"]["sourcing"].update(model="claude-sonnet-5-5"))
    rt = make_runtime(provider=fixed(Usage(tokens_in=1000, tokens_out=1000)))
    res = adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert rt.fake.requests[0].model == "claude-sonnet-5-5"
    assert rows(rt)[0]["model"] == "claude-sonnet-5-5"
    assert res.cost_usd == pytest.approx((1000 * 2 + 1000 * 10) / 1e6)   # tarif Sonnet appliqué


# --- critère : plafond global puis plafond par boutique ---

def test_global_cap_refuses_and_alerts(rt):
    seed_spend(rt, 60.0, shop_id="autre")
    with pytest.raises(BudgetExceeded) as exc:
        adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert exc.value.cap.key == "prod:global"
    assert rt.fake.requests == []                       # rien n'est parti chez le fournisseur
    assert rows(rt, status="refused")[0]["cost_usd"] == 0
    assert any("refusé" in m and "global" in m for m in rt.notifier.messages)


def test_shop_cap_refuses_and_alerts(rt):
    seed_spend(rt, 45.0, shop_id="shop-a")
    with pytest.raises(BudgetExceeded) as exc:
        adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert exc.value.cap.key == "prod:shop:shop-a"
    assert any("refusé" in m and "shop-a" in m for m in rt.notifier.messages)
    adapter.call("sourcing", "shop-b", MSG, runtime=rt)  # une autre boutique passe


def test_shop_override(config_dir, make_runtime):
    edit_yaml(config_dir / "agents.yaml",
              lambda d: d["budgets"]["prod"]["shop_overrides"].update({"shop-a": 1}))
    rt = make_runtime()
    seed_spend(rt, 1.0, shop_id="shop-a")
    with pytest.raises(BudgetExceeded):
        adapter.call("sourcing", "shop-a", MSG, runtime=rt)


def test_refusal_alert_deduplicated_per_day(rt):
    seed_spend(rt, 60.0)
    for _ in range(3):
        with pytest.raises(BudgetExceeded):
            adapter.call("sourcing", None, MSG, runtime=rt)
    assert sum("refusé" in m for m in rt.notifier.messages) == 1
    assert len(rows(rt, status="refused")) == 3         # chaque refus reste journalisé


# --- critère : alerte à 80 %, une seule fois par mois et par plafond ---

def test_80pct_alert_once_per_month_and_cap(config_dir, make_runtime):
    edit_yaml(config_dir / "agents.yaml", lambda d: d["budgets"]["prod"].update(global_monthly=1))
    rt = make_runtime(provider=fixed(Usage(tokens_in=10, tokens_out=10)))
    seed_spend(rt, 0.85)
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    alerts = [m for m in rt.notifier.messages if "80 %" in m]
    assert len(alerts) == 1 and "prod:global" in alerts[0]
    rt.clock.advance(days=30)                           # décembre : nouveau mois
    seed_spend(rt, 0.9)
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert sum("80 %" in m for m in rt.notifier.messages) == 2


def test_80pct_alert_is_per_cap(rt):
    seed_spend(rt, 40.0, shop_id="shop-a")              # 40/45 boutique, 40/60 global
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    alerts = [m for m in rt.notifier.messages if "80 %" in m]
    assert len(alerts) == 1 and "prod:shop:shop-a" in alerts[0]
    seed_spend(rt, 10.0, shop_id="shop-b")              # global : 50/60 > 80 %
    adapter.call("sourcing", "shop-b", MSG, runtime=rt)
    alerts = [m for m in rt.notifier.messages if "80 %" in m]
    assert len(alerts) == 2 and "prod:global" in alerts[1]


# --- critère : prod et eval séparés ---

def test_eval_and_prod_spend_are_isolated(make_runtime):
    rt = make_runtime(provider=fixed(Usage(tokens_in=1000, tokens_out=1000)))
    month = db.month_key(rt.clock())
    adapter.call("sourcing", "shop-a", MSG, purpose="eval", runtime=rt)
    assert db.month_spend(rt.conn, month, "prod") == 0
    eval_spend = db.month_spend(rt.conn, month, "eval")
    assert eval_spend > 0
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert db.month_spend(rt.conn, month, "eval") == eval_spend
    assert db.month_spend(rt.conn, month, "prod") > 0


def test_eval_cap_independent_of_prod(rt):
    seed_spend(rt, 5.0, purpose="eval")
    with pytest.raises(BudgetExceeded) as exc:
        adapter.call("sourcing", "shop-a", MSG, purpose="eval", runtime=rt)
    assert exc.value.cap.key == "eval:global"
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)  # la prod n'est pas bloquée
    seed_spend(rt, 60.0)
    rt.conn.execute("DELETE FROM llm_calls WHERE purpose='eval'")
    adapter.call("sourcing", "shop-a", MSG, purpose="eval", runtime=rt)  # l'eval n'est pas bloquée par la prod


# --- critère : outil web réservé à veille ---

def test_web_tool_refused_for_other_agents(rt):
    for agent in ("sourcing", "sav", "boutique", "contenu", "marketing"):
        with pytest.raises(ToolNotAllowed):
            adapter.call(agent, "shop-a", MSG, tools=[WEB_TOOL], mode="sync", runtime=rt)
    assert rt.fake.requests == []
    assert len(rows(rt, status="refused")) == 5


def test_web_tool_refused_even_if_listed_in_config(config_dir, make_runtime):
    edit_yaml(config_dir / "agents.yaml",
              lambda d: d["agents"]["sav"].update(allowed_tools=["web_search"]))
    rt = make_runtime()
    with pytest.raises(ToolNotAllowed):
        adapter.call("sav", "shop-a", MSG, tools=[WEB_TOOL], runtime=rt)


def test_web_tool_allowed_for_veille(rt):
    adapter.call("veille", None, MSG, tools=[WEB_TOOL], mode="sync", runtime=rt)
    assert rt.fake.requests[0].tools == [WEB_TOOL]


def test_unlisted_tool_refused_for_veille(rt):
    with pytest.raises(ToolNotAllowed, match="allowed_tools"):
        adapter.call("veille", None, MSG, tools=[{"name": "send_email"}], mode="sync", runtime=rt)
    # même un outil web est refusé à veille s'il n'est pas dans sa liste (seul web_search y est)
    with pytest.raises(ToolNotAllowed, match="allowed_tools"):
        adapter.call("veille", None, MSG, tools=[{"type": "web_fetch_20260209", "name": "web_fetch"}],
                     mode="sync", runtime=rt)
    assert rt.fake.requests == []


def test_neutral_named_tool_refused_outside_veille(rt):
    # « fetch_page » échappe à la détection par mots-clés : c'est la liste blanche qui le bloque.
    for agent in ("sourcing", "sav", "boutique", "contenu", "marketing"):
        with pytest.raises(ToolNotAllowed, match="allowed_tools"):
            adapter.call(agent, "shop-a", MSG, tools=[{"name": "fetch_page"}], mode="sync", runtime=rt)
    assert rt.fake.requests == []


def test_agent_without_list_has_no_tools(config_dir, make_runtime):
    edit_yaml(config_dir / "agents.yaml", lambda d: d["agents"]["veille"].pop("allowed_tools"))
    rt = make_runtime()
    assert rt.settings.agent("veille").allowed_tools == ()
    with pytest.raises(ToolNotAllowed):
        adapter.call("veille", None, MSG, tools=[WEB_TOOL], mode="sync", runtime=rt)
    adapter.call("veille", None, MSG, mode="sync", runtime=rt)   # sans outil : l'appel passe


# --- critère : tarif expiré ou absent = alerte sans blocage ---

def _with_prices(rt, model, **changes):
    price = dataclasses.replace(rt.prices.models[model], **changes)
    rt.prices = dataclasses.replace(rt.prices, models={**rt.prices.models, model: price})


def test_expired_price_alerts_but_does_not_block(make_runtime):
    rt = make_runtime(provider=fixed(Usage(tokens_in=1000, tokens_out=500)))
    _with_prices(rt, "claude-haiku-5-5", valid_until=dt.date(2026, 10, 1))
    res = adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert res.cost_usd == pytest.approx(0.00035)       # dernier tarif connu
    assert any("expiré" in m and "claude-haiku-5-5" in m for m in rt.notifier.messages)


def test_missing_price_alerts_but_does_not_block(config_dir, make_runtime):
    edit_yaml(config_dir / "agents.yaml", lambda d: d["agents"]["sourcing"].update(model="claude-nouveau"))
    rt = make_runtime()
    res = adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert res.call_id
    assert any("absent" in m and "claude-nouveau" in m for m in rt.notifier.messages)


# --- règle 4 : prompt > 100 000 tokens ---

def test_long_prompt_alert(make_runtime):
    rt = make_runtime(provider=fixed(Usage(tokens_in=120_000, tokens_out=10)))
    adapter.call("sav", "shop-a", MSG, runtime=rt)
    assert any("100 000" in m for m in rt.notifier.messages)


# --- règle 6 : 2 nouvelles tentatives maximum, délai croissant, chacune journalisée ---

def test_retries_then_success(make_runtime):
    fake = FakeProvider(failures=[ProviderError("surcharge 529", True), ProviderError("timeout", True)])
    rt = make_runtime(provider=fake)
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    statuses = [(r["attempt"], r["status"]) for r in rows(rt)]
    assert statuses == [(1, "error"), (2, "error"), (3, "ok")]
    assert rt.sleeps == [2, 4]


def test_gives_up_after_two_retries(make_runtime):
    fake = FakeProvider(failures=[ProviderError("529", True)] * 3 + [ProviderError("jamais atteint", True)])
    rt = make_runtime(provider=fake)
    with pytest.raises(adapter.LLMCallError):
        adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert len(rows(rt, status="error")) == 3
    assert len(fake.requests) == 3


def test_non_retryable_error_not_retried(make_runtime):
    rt = make_runtime(provider=FakeProvider(failures=[ProviderError("400 invalide", False)]))
    with pytest.raises(adapter.LLMCallError):
        adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert len(rows(rt)) == 1 and rt.sleeps == []


def test_errors_are_redacted_in_journal(make_runtime):
    leak = f"clé {fake_anthropic_key()} client jean@REDACTED.invalid"
    rt = make_runtime(provider=FakeProvider(failures=[ProviderError(leak, False)]))
    with pytest.raises(adapter.LLMCallError) as exc:
        adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    err = rows(rt)[0]["error"]
    assert ANTHROPIC_KEY_PREFIX not in err and "jean@REDACTED.invalid" not in err
    assert ANTHROPIC_KEY_PREFIX not in str(exc.value)


# --- sortie structurée ---

SCHEMA = {"type": "object", "required": ["prix"], "properties": {"prix": {"type": "number"}},
          "additionalProperties": False}


def test_schema_output_validated(make_runtime):
    rt = make_runtime(provider=fixed(Usage(tokens_in=10, tokens_out=10), text='{"prix": 19.9}'))
    res = adapter.call("sourcing", "shop-a", MSG, schema=SCHEMA, runtime=rt)
    assert res.content == {"prix": 19.9}


def test_schema_violation_is_format_error(make_runtime):
    rt = make_runtime(provider=fixed(Usage(tokens_in=10, tokens_out=10), text='{"prix": "cher"}'))
    with pytest.raises(adapter.LLMFormatError):
        adapter.call("sourcing", "shop-a", MSG, schema=SCHEMA, runtime=rt)
    (row,) = rows(rt)
    assert row["status"] == "format_error" and row["cost_usd"] > 0   # le coût reste compté


# --- dry-run par défaut ---

def test_dry_run_uses_fake_and_does_not_count(make_runtime):
    real = FakeProvider()
    rt = make_runtime(live=False, provider=real)
    res = adapter.call("sourcing", "shop-a", MSG, schema=SCHEMA, runtime=rt)
    assert real.requests == []                          # le fournisseur configuré n'est pas appelé
    assert res.dry_run is True and res.content == {"prix": 0}
    (row,) = rows(rt)
    assert row["dry_run"] == 1 and row["provider"] == "anthropic"
    assert db.month_spend(rt.conn, db.month_key(rt.clock()), "prod") == 0


def test_unknown_agent_and_bad_inputs(rt):
    with pytest.raises(Exception, match="inconnu"):
        adapter.call("pirate", None, MSG, runtime=rt)
    with pytest.raises(ValueError):
        adapter.call("sav", None, MSG, purpose="test", runtime=rt)
    with pytest.raises(ValueError):
        adapter.call("sav", None, MSG, mode="stream", runtime=rt)
