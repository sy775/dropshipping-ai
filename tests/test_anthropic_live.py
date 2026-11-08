"""Test réel unique (à lancer à la main) : un appel claude-haiku-5-5, purpose="eval".

    LLM_LIVE=1 python -m pytest -m live tests/test_anthropic_live.py -s

Ignoré sauf si LLM_LIVE=1 et ANTHROPIC_API_KEY sont définies (environnement ou .env). Coût attendu :
quelques dixièmes de centime (max_tokens = 512). L'appel est écrit dans le journal réel (JOURNAL_DB_PATH,
sinon data/journal.sqlite) : il compte dans le plafond eval de 5 $, comme toute dépense réelle.
"""
import dataclasses
import os

import pytest

from llm.runtime import load_env

load_env()   # override=False : une variable déjà exportée l'emporte sur .env

LIVE = os.environ.get("LLM_LIVE") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="test réel : définir LLM_LIVE=1 et ANTHROPIC_API_KEY"),
]


def test_live_haiku_eval_call():
    from llm import adapter
    from llm.providers.anthropic import AnthropicProvider, map_usage
    from llm.runtime import Runtime

    rt = Runtime.from_env()
    assert rt.live, "LLM_LIVE=1 attendu"
    sav = dataclasses.replace(rt.settings.agents["sav"], model="claude-haiku-5-5", max_tokens=512, effort="low")
    rt.settings = dataclasses.replace(rt.settings, agents={**rt.settings.agents, "sav": sav})

    raw = []
    provider = AnthropicProvider(rt.settings.providers.get("anthropic", {}), rt.alert_once)
    create = provider.client.messages.create

    def recording(**params):
        msg = create(**params)
        raw.append(msg)
        return msg
    provider.client.messages.create = recording
    rt.provider_factory = lambda name: provider

    res = adapter.call("sav", None, [{"role": "user", "content": "Réponds seulement par le mot : bonjour"}],
                       purpose="eval", runtime=rt)

    (msg,) = raw
    assert msg.stop_reason == "end_turn"
    u = msg.usage
    # Calcul manuel sur l'usage brut, tarif Haiku 5.5 <= 100 000 tokens (prices.yaml, vérifié le 2026-11-08)
    searches = getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
    manual = (u.input_tokens * 0.10 + u.output_tokens * 0.50
              + (u.cache_creation_input_tokens or 0) * 0.125 + (u.cache_read_input_tokens or 0) * 0.01) / 1e6 \
        + searches * 0.01
    assert res.usage == map_usage(u)
    assert res.cost_usd == pytest.approx(manual, rel=1e-9)
    row = rt.conn.execute("SELECT * FROM llm_calls WHERE call_id = ?", (res.call_id,)).fetchone()
    assert row["purpose"] == "eval" and row["dry_run"] == 0 and row["stop_reason"] == "end_turn"
    print(f"\ncall_id={res.call_id} coût={res.cost_usd:.6f} $ usage={res.usage} réponse={res.content!r}")
