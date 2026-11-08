"""Session 2, point 1 : motifs d'arrêt, coût des recherches web, effort, isolation de l'adaptateur."""
import ast
import math
from pathlib import Path

import pytest

from journal import db
from llm import adapter, guards
from llm.adapter import LLMIncompleteError, LLMRefusedError
from llm.config import ConfigError, load_prices, load_settings
from llm.pricing import Usage, compute_cost, payload_chars
from llm.providers.base import BatchItemResult, ProviderResponse
from llm.providers.fake import FakeProvider
from tests.conftest import edit_yaml, rows

REPO = Path(__file__).resolve().parents[1]
MSG = [{"role": "user", "content": "Analyse la niche bougies"}]
WEB = {"type": "web_search_20250305", "name": "web_search"}
USAGE = Usage(tokens_in=1000, tokens_out=500)
HAIKU_COST = (1000 * 0.10 + 500 * 0.50) / 1e6


def answering(stop_reason, text="réponse", usage=USAGE):
    return FakeProvider(responder=lambda req: ProviderResponse(text=text, usage=usage, stop_reason=stop_reason))


# ---------------------------------------------------------------- motifs d'arrêt (sync)

def test_provider_response_requires_explicit_stop_reason():
    with pytest.raises(TypeError, match="stop_reason"):
        ProviderResponse(text="x", usage=USAGE)
    assert ProviderResponse(text="x", usage=USAGE, stop_reason=None).stop_reason is None


def test_fake_and_stub_providers_set_stop_reason_explicitly():
    from llm.providers.base import ProviderRequest
    from llm.providers.fake import default_responder
    from llm.providers.stub_alt import StubAltProvider
    req = ProviderRequest(model="m", messages=MSG, max_tokens=10, purpose="eval")
    assert default_responder(req).stop_reason == "end_turn"
    assert StubAltProvider().complete(req).stop_reason == "end_turn"


@pytest.mark.parametrize("stop", ["end_turn", "stop_sequence"])
def test_success_stop_reasons_return_content(make_runtime, stop):
    rt = make_runtime(provider=answering(stop))
    res = adapter.call("sav", "shop-a", MSG, runtime=rt)
    assert res.content == "réponse"
    (row,) = rows(rt, call_id=res.call_id)
    assert row["status"] == "ok" and row["stop_reason"] == stop


@pytest.mark.parametrize("stop", ["max_tokens", "model_context_window_exceeded", "pause_turn", "tool_use", None])
def test_incomplete_stop_reasons_raise_without_content_and_log_cost(make_runtime, stop):
    rt = make_runtime(provider=answering(stop))
    with pytest.raises(LLMIncompleteError) as err:
        adapter.call("sav", "shop-a", MSG, runtime=rt)
    assert err.value.stop_reason == stop
    assert not hasattr(err.value, "content")
    (row,) = rows(rt, call_id=err.value.call_id)
    assert row["status"] == "incomplete" and row["stop_reason"] == stop
    assert row["cost_usd"] == pytest.approx(HAIKU_COST)        # le coût réel est toujours journalisé
    assert len(rt.fake.requests) == 1                          # aucune nouvelle tentative
    assert db.month_spend(rt.conn, "2026-11", "prod") == pytest.approx(HAIKU_COST)


def test_refusal_raises_logs_cost_no_retry_and_alerts_once_per_day(make_runtime):
    rt = make_runtime(provider=answering("refusal", text="contenu à ne pas renvoyer"))
    for _ in range(2):
        with pytest.raises(LLMRefusedError) as err:
            adapter.call("sav", "shop-a", MSG, runtime=rt)
        assert "contenu à ne pas renvoyer" not in str(err.value)
    refused = rows(rt, status="refused")
    assert len(refused) == 2 and all(r["stop_reason"] == "refusal" for r in refused)
    assert all(r["cost_usd"] == pytest.approx(HAIKU_COST) for r in refused)
    assert len(rt.fake.requests) == 2                          # une requête par appel, aucune reprise
    assert sum("Refus du modèle" in m for m in rt.notifier.messages) == 1
    rt.clock.advance(days=1)
    with pytest.raises(LLMRefusedError):
        adapter.call("sav", "shop-a", MSG, runtime=rt)
    assert sum("Refus du modèle" in m for m in rt.notifier.messages) == 2


def test_schema_not_checked_on_incomplete_answer(make_runtime):
    rt = make_runtime(provider=answering("max_tokens", text='{"prix": 1'))
    with pytest.raises(LLMIncompleteError):
        adapter.call("sav", "shop-a", MSG, schema={"type": "object"}, runtime=rt)
    assert rows(rt)[0]["status"] == "incomplete"


def test_migration_004_documents_statuses(rt):
    statuses = {r["status"]: r["description"] for r in rt.conn.execute("SELECT * FROM llm_call_statuses")}
    assert "incomplete" in statuses
    assert "stop_reason = refusal" in statuses["refused"]


# ---------------------------------------------------------------- motifs d'arrêt (lot)

def test_batch_items_classified_by_stop_reason(make_runtime):
    stops = iter(["end_turn", "max_tokens", "refusal"])
    rt = make_runtime(provider=FakeProvider(
        responder=lambda req: ProviderResponse(text='{"ok": true}', usage=USAGE, stop_reason=next(stops))))
    h = adapter.call("veille", None, [MSG, MSG, MSG], runtime=rt)
    rt.fake.finish(h.batch_id)
    out = adapter.collect_batch(h.batch_id, schema={"type": "object"}, runtime=rt)
    ok, cut, refused = (out[f"item-{i}"] for i in range(3))
    assert ok.content == {"ok": True} and ok.error is None
    assert cut.content is None and "max_tokens" in cut.error
    assert refused.content is None and "refusal" in refused.error
    assert {r["status"] for r in rows(rt, batch_id=h.batch_id) if r["mode"] == "batch"} >= {"ok", "incomplete",
                                                                                            "refused"}
    assert all(o.cost_usd > 0 for o in out.values())          # coûts réels journalisés pour chaque élément
    assert sum("Refus du modèle" in m for m in rt.notifier.messages) == 1


# ---------------------------------------------------------------- recherches web : coût et estimation

def _prices(config_dir):
    return load_prices(config_dir)


def test_web_search_cost_added_and_not_batch_discounted(config_dir):
    sonnet = _prices(config_dir).models["claude-sonnet-5-5"]
    assert sonnet.web_search_per_1000 == 10.0
    u = Usage(tokens_in=1000, tokens_out=1000, web_search_requests=3)
    tokens = (1000 * 2 + 1000 * 10) / 1e6
    assert compute_cost(sonnet, u) == pytest.approx(tokens + 0.03)
    assert compute_cost(sonnet, u, batch=True) == pytest.approx(tokens * 0.5 + 0.03)


def test_web_search_requests_are_journaled_and_billed(make_runtime):
    usage = Usage(tokens_in=1000, tokens_out=100, web_search_requests=2)
    rt = make_runtime(provider=answering("end_turn", usage=usage))
    res = adapter.call("veille", None, MSG, tools=[WEB], mode="sync", runtime=rt)
    (row,) = rows(rt, call_id=res.call_id)
    assert row["web_search_requests"] == 2
    assert row["cost_usd"] == pytest.approx((1000 * 2 + 100 * 10) / 1e6 + 0.02)


def _expected_estimate(settings, tools, batch=False):
    raw = math.ceil(payload_chars(MSG, None, None, tools) / settings.chars_per_token)
    searches = settings.agents["veille"].web_search_max_uses
    input_est = math.ceil(raw * (1 + settings.input_margin)) + searches * settings.search_tokens_estimate
    tokens = (input_est * 2.0 + settings.agents["veille"].max_tokens * 10.0) / 1e6
    return tokens * (0.5 if batch else 1) + searches * 0.01


def test_estimate_includes_max_searches_and_search_tokens(make_runtime, config_dir):
    rt = make_runtime()
    res = adapter.call("veille", None, MSG, tools=[WEB], mode="sync", runtime=rt)
    sent = rt.fake.requests[0].tools
    assert sent == [{**WEB, "max_uses": 5}]
    row = rows(rt, call_id=res.call_id)[0]
    assert row["tokens_in_estimated"] > 5 * 4000
    assert row["cost_estimated_usd"] == pytest.approx(_expected_estimate(rt.settings, sent))


def test_estimate_without_web_tool_has_no_search_part(make_runtime):
    rt = make_runtime()
    res = adapter.call("veille", None, MSG, mode="sync", runtime=rt)
    assert rows(rt, call_id=res.call_id)[0]["tokens_in_estimated"] < 4000


def test_caller_cannot_raise_max_uses(make_runtime):
    rt = make_runtime()
    adapter.call("veille", None, MSG, tools=[{**WEB, "max_uses": 50}], mode="sync", runtime=rt)
    assert rt.fake.requests[0].tools[0]["max_uses"] == 5


def test_veille_batch_reservation_includes_searches(make_runtime):
    rt = make_runtime()
    h = adapter.call("veille", None, [MSG, MSG], tools=[WEB], runtime=rt)
    sent = rt.fake.batches[h.batch_id][0][1].tools
    assert h.reserved_usd == pytest.approx(2 * _expected_estimate(rt.settings, sent, batch=True))
    assert rows(rt, status="reserved")[0]["cost_usd"] == pytest.approx(h.reserved_usd)


def test_batch_with_searches_refused_when_cap_too_low(config_dir, make_runtime):
    # 2 éléments x 5 recherches = 0,10 $ de frais de recherche seuls : plafond à 0,05 $ -> lot non envoyé
    edit_yaml(config_dir / "agents.yaml", lambda d: d["budgets"]["prod"].update(global_monthly=0.05))
    rt = make_runtime()
    with pytest.raises(adapter.BudgetExceeded):
        adapter.call("veille", None, [MSG, MSG], tools=[WEB], runtime=rt)
    assert rt.fake.batches == {}


# ---------------------------------------------------------------- config : effort et max_uses

def test_effort_is_passed_to_provider(rt):
    adapter.call("sav", "shop-a", MSG, runtime=rt)
    assert rt.fake.requests[0].effort == "low"


def test_invalid_effort_rejected(config_dir):
    edit_yaml(config_dir / "agents.yaml", lambda d: d["agents"]["sav"].update(effort="minimal"))
    with pytest.raises(ConfigError, match="effort"):
        load_settings(config_dir)


def test_web_search_without_max_uses_rejected(config_dir):
    edit_yaml(config_dir / "agents.yaml", lambda d: d["agents"]["veille"].pop("web_search_max_uses"))
    with pytest.raises(ConfigError, match="web_search_max_uses"):
        load_settings(config_dir)


@pytest.mark.parametrize("tool", ["web_fetch", "web_search"])
def test_any_web_tool_without_max_uses_rejected_by_config(config_dir, tool):
    def mutate(d):
        d["agents"]["veille"].pop("web_search_max_uses")
        d["agents"]["veille"]["allowed_tools"] = [tool]
    edit_yaml(config_dir / "agents.yaml", mutate)
    with pytest.raises(ConfigError, match=f"{tool}.*web_search_max_uses"):
        load_settings(config_dir)


def test_non_web_tool_does_not_need_max_uses(config_dir):
    edit_yaml(config_dir / "agents.yaml", lambda d: d["agents"]["sav"].update(allowed_tools=["lookup_order"]))
    assert load_settings(config_dir).agents["sav"].web_search_max_uses is None


def test_adapter_refuses_web_tool_without_max_uses_even_if_config_bypassed(make_runtime):
    # Settings construit à la main : la validation de agents.yaml n'a pas tourné.
    import dataclasses
    rt = make_runtime()
    veille = dataclasses.replace(rt.settings.agents["veille"], allowed_tools=("web_fetch",), web_search_max_uses=None)
    rt.settings = dataclasses.replace(rt.settings, agents={**rt.settings.agents, "veille": veille})
    with pytest.raises(guards.ToolNotAllowed, match="web_search_max_uses"):
        adapter.call("veille", None, MSG, tools=[{"type": "web_fetch_20260209", "name": "web_fetch"}],
                     mode="sync", runtime=rt)
    assert rt.fake.requests == [] and rt.fake.batches == {}
    (row,) = rows(rt)
    assert row["status"] == "refused" and row["cost_usd"] == 0


def test_search_tokens_estimate_required(config_dir):
    edit_yaml(config_dir / "agents.yaml", lambda d: d["estimation"].pop("search_tokens_estimate"))
    with pytest.raises(ConfigError, match="search_tokens_estimate"):
        load_settings(config_dir)


# ---------------------------------------------------------------- isolation : aucun fournisseur importé

def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


@pytest.mark.parametrize("module", ["llm/adapter.py", "llm/guards.py"])
def test_adapter_and_guards_import_no_provider(module):
    imported = _imports(REPO / module)
    for name in imported:
        assert not name.startswith("anthropic"), f"{module} importe le SDK {name}"
        if name.startswith("llm.providers"):
            assert name == "llm.providers.base" or name.startswith("llm.providers.base."), \
                f"{module} importe un fournisseur : {name}"
