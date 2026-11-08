"""Session 2, point 2 : fournisseur Anthropic, testé sans réseau avec un faux client SDK."""
import logging
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from alerts.telegram import MemoryNotifier
from journal import db
from llm import adapter
from llm.config import ConfigError, load_prices, load_settings
from llm.pricing import Usage
from llm.providers import create_provider
from llm.providers.anthropic import WEB_SEARCH_TYPE, AnthropicProvider, build_params, map_message
from llm.providers.base import ProviderError, ProviderRequest
from llm.runtime import Runtime
from tests.conftest import NOW, Clock, NetworkBlocked, edit_yaml, rows
from tests.fakes import fake_anthropic_key

MSG = [{"role": "user", "content": "Bonjour"}]
REQ = ProviderRequest(model="claude-haiku-5-5", messages=MSG, max_tokens=100)
URL = "https://api.anthropic.com/v1/messages"


# ---------------------------------------------------------------- faux client SDK

def message(text="ok", stop="end_turn", usage=None):
    usage = usage or NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                        cache_creation_input_tokens=0, server_tool_use=None)
    return NS(content=[NS(type="text", text=text)], stop_reason=stop, usage=usage)


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.batches = FakeBatches()

    def create(self, **params):
        self.calls.append(params)
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


class FakeBatches:
    def __init__(self):
        self.created, self.status, self.items = [], "in_progress", []

    def create(self, requests):
        self.created.append(requests)
        return NS(id="msgbatch_test")

    def retrieve(self, batch_id):
        return NS(id=batch_id, processing_status=self.status)

    def results(self, batch_id):
        return iter(self.items)


def provider(outcomes=(), alerts=None):
    client = NS(messages=FakeMessages(outcomes or [message()]))
    sink = alerts if alerts is not None else []
    return AnthropicProvider({"timeout_s": 30}, lambda key, text: sink.append((key, text)), client=client), client


def status_error(cls, code, text="erreur"):
    req = httpx2.Request("POST", URL)
    return cls(text, response=httpx2.Response(code, request=req), body=None)


# ---------------------------------------------------------------- erreurs

@pytest.mark.parametrize("exc, retryable", [
    (lambda: status_error(anthropic.RateLimitError, 429), True),
    (lambda: status_error(anthropic.InternalServerError, 500), True),
    (lambda: status_error(anthropic.ServiceUnavailableError, 503), True),
    (lambda: status_error(anthropic.OverloadedError, 529), True),
    (lambda: anthropic.APIConnectionError(request=httpx2.Request("POST", URL)), True),
    (lambda: anthropic.APITimeoutError(request=httpx2.Request("POST", URL)), True),
    (lambda: status_error(anthropic.BadRequestError, 400), False),
    (lambda: status_error(anthropic.NotFoundError, 404), False),
    (lambda: status_error(anthropic.PermissionDeniedError, 403), False),
    (lambda: status_error(anthropic.UnprocessableEntityError, 422), False),
])
def test_error_mapping(exc, retryable):
    alerts = []
    p, _ = provider([exc()], alerts)
    with pytest.raises(ProviderError) as err:
        p.complete(REQ)
    assert err.value.retryable is retryable
    assert alerts == []


def test_auth_error_not_retryable_and_alerts():
    alerts = []
    p, _ = provider([status_error(anthropic.AuthenticationError, 401)], alerts)
    with pytest.raises(ProviderError) as err:
        p.complete(REQ)
    assert err.value.retryable is False
    assert len(alerts) == 1 and alerts[0][0] == "anthropic:auth"


def test_auth_alert_deduplicated_per_day_through_runtime(config_dir):
    rt = Runtime(settings=load_settings(config_dir), prices=load_prices(config_dir), conn=db.connect(":memory:"),
                 notifier=MemoryNotifier(), live=True, clock=Clock(NOW), sleep=lambda s: None)
    for _ in range(3):
        rt.alert_once("anthropic:auth", "auth KO")
    assert rt.notifier.messages == ["auth KO"]
    rt.clock.advance(days=1)
    rt.alert_once("anthropic:auth", "auth KO")
    assert len(rt.notifier.messages) == 2


def test_adapter_retries_retryable_provider_errors(make_runtime):
    p, client = provider([status_error(anthropic.OverloadedError, 529), message("enfin")])
    rt = make_runtime()
    rt.provider_factory = lambda name: p
    res = adapter.call("sav", "shop-a", MSG, runtime=rt)
    assert res.content == "enfin" and len(client.messages.calls) == 2
    assert [r["status"] for r in rows(rt)] == ["error", "ok"]


def test_no_key_in_errors_or_logs(monkeypatch, caplog):
    key = fake_anthropic_key()
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    p, _ = provider([status_error(anthropic.AuthenticationError, 401, f"invalid x-api-key {key}"),
                     anthropic.APIConnectionError(message=f"proxy refused Bearer {key}",
                                                  request=httpx2.Request("POST", URL))])
    caplog.set_level(logging.DEBUG)
    messages = []
    for _ in range(2):
        with pytest.raises(ProviderError) as err:
            p.complete(REQ)
        messages.append(str(err.value))
    assert all(key not in m and "[SECRET]" in m for m in messages)
    assert key not in caplog.text


def test_sdk_client_built_without_retries_and_with_timeout(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_anthropic_key())
    client = AnthropicProvider({"timeout_s": 45}).client
    assert client.max_retries == 0
    assert client.timeout == 45


# ---------------------------------------------------------------- paramètres de requête

def test_cache_control_on_system_prefix_only():
    params = build_params(ProviderRequest(model="m", messages=MSG, max_tokens=10, system="PLAYBOOK"))
    assert params["system"] == [{"type": "text", "text": "PLAYBOOK", "cache_control": {"type": "ephemeral"}}]
    assert "ttl" not in params["system"][0]["cache_control"]          # cache à 5 minutes
    assert all("cache_control" not in str(m) for m in params["messages"])
    assert "system" not in build_params(REQ)


def test_effort_schema_and_temperature():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
              "additionalProperties": False}
    params = build_params(ProviderRequest(model="m", messages=MSG, max_tokens=10, schema=schema, effort="low"))
    assert params["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": schema}}
    assert "temperature" not in params
    assert "output_config" not in build_params(REQ)
    assert build_params(ProviderRequest(model="m", messages=MSG, max_tokens=10, temperature=0.0))["temperature"] == 0


def test_only_adapter_approved_tools_are_sent_web_search_simple_version():
    tools = [{"name": "web_search", "max_uses": 5}]
    params = build_params(ProviderRequest(model="m", messages=MSG, max_tokens=10, tools=tools))
    assert params["tools"] == [{"type": WEB_SEARCH_TYPE, "name": "web_search", "max_uses": 5}]
    assert WEB_SEARCH_TYPE == "web_search_20250305"
    assert "tools" not in build_params(REQ)


def test_complete_sends_params_to_sdk():
    p, client = provider()
    p.complete(ProviderRequest(model="claude-haiku-5-5", messages=MSG, max_tokens=64, effort="low"))
    sent = client.messages.calls[0]
    assert sent["model"] == "claude-haiku-5-5" and sent["max_tokens"] == 64
    assert sent["output_config"] == {"effort": "low"}


# ---------------------------------------------------------------- usage

def test_usage_mapping_with_cache_read_write_and_search():
    usage = NS(input_tokens=120, output_tokens=40, cache_read_input_tokens=3000, cache_creation_input_tokens=800,
               server_tool_use=NS(web_search_requests=2, web_fetch_requests=0))
    resp = map_message(NS(content=[NS(type="thinking", thinking=""), NS(type="text", text="a"),
                                   NS(type="server_tool_use", name="web_search"), NS(type="text", text="b")],
                          stop_reason="end_turn", usage=usage))
    assert resp.text == "ab"
    assert resp.usage == Usage(tokens_in=120, tokens_out=40, cache_read=3000, cache_write=800,
                               web_search_requests=2)
    # taille du prompt au sens tarifaire = input + cache lu + cache écrit (doc prompt caching)
    assert resp.usage.prompt_tokens == 3920


def test_usage_mapping_tolerates_missing_fields():
    resp = map_message(NS(content=[], stop_reason="max_tokens",
                          usage=NS(input_tokens=5, output_tokens=0, cache_read_input_tokens=None,
                                   cache_creation_input_tokens=None, server_tool_use=None)))
    assert resp.usage == Usage(tokens_in=5) and resp.stop_reason == "max_tokens"


def test_cost_from_mapped_usage_matches_manual(config_dir):
    from llm.pricing import compute_cost
    haiku = load_prices(config_dir).models["claude-haiku-5-5"]
    u = map_message(message(usage=NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=5000,
                                     cache_creation_input_tokens=2000,
                                     server_tool_use=NS(web_search_requests=1)))).usage
    manual = (1000 * 0.10 + 200 * 0.50 + 5000 * 0.01 + 2000 * 0.125) / 1e6 + 0.01
    assert compute_cost(haiku, u) == pytest.approx(manual)


# ---------------------------------------------------------------- lots

def test_batch_submit_and_results_mapping():
    p, client = provider()
    items = [(f"item-{i}", REQ) for i in range(4)]
    assert p.submit_batch(items) == "msgbatch_test"
    sent = client.messages.batches.created[0]
    assert [r["custom_id"] for r in sent] == ["item-0", "item-1", "item-2", "item-3"]
    assert sent[0]["params"]["model"] == "claude-haiku-5-5"

    assert p.batch_results("msgbatch_test") is None                     # pas encore terminé
    client.messages.batches.status = "ended"
    client.messages.batches.items = [   # ordre quelconque : l'appelant indexe par custom_id
        NS(custom_id="item-2", result=NS(type="canceled")),
        NS(custom_id="item-0", result=NS(type="succeeded", message=message("salut"))),
        NS(custom_id="item-1", result=NS(type="errored", error=NS(error=NS(type="invalid_request_error",
                                                                              message="max_tokens trop grand")))),
        NS(custom_id="item-3", result=NS(type="expired")),
    ]
    out = {r.custom_id: r for r in p.batch_results("msgbatch_test")}
    assert out["item-0"].response.text == "salut" and out["item-0"].error is None
    assert out["item-1"].response is None and "invalid_request_error" in out["item-1"].error
    assert out["item-2"].response is None and out["item-2"].error == "canceled"
    assert out["item-3"].response is None and out["item-3"].error == "expired"


def test_batch_errors_are_mapped():
    p, client = provider()
    client.messages.batches.retrieve = lambda bid: (_ for _ in ()).throw(status_error(anthropic.NotFoundError, 404))
    with pytest.raises(ProviderError) as err:
        p.batch_results("msgbatch_inconnu")
    assert err.value.retryable is False


# ---------------------------------------------------------------- registre, config, isolation

def test_registered_and_built_from_config(config_dir):
    settings = load_settings(config_dir)
    p = create_provider("anthropic", settings.providers["anthropic"], lambda k, t: None)
    assert isinstance(p, AnthropicProvider) and p.timeout_s == 120


def test_runtime_default_factory_passes_options(config_dir):
    rt = Runtime(settings=load_settings(config_dir), prices=load_prices(config_dir), conn=db.connect(":memory:"),
                 notifier=MemoryNotifier(), live=True)
    p = rt.provider_for("anthropic")
    assert isinstance(p, AnthropicProvider) and p.timeout_s == 120


@pytest.mark.parametrize("timeout, ttl", [(900, 15), (901, 15), (120, 2)])
def test_timeout_not_below_pending_ttl_rejected(config_dir, timeout, ttl):
    edit_yaml(config_dir / "agents.yaml", lambda d: d.update(reservations={"pending_ttl_minutes": ttl},
                                                            providers={"anthropic": {"timeout_s": timeout}}))
    with pytest.raises(ConfigError, match="timeout_s"):
        load_settings(config_dir)


def test_timeout_below_ttl_accepted(config_dir):
    edit_yaml(config_dir / "agents.yaml", lambda d: d.update(providers={"anthropic": {"timeout_s": 899}}))
    assert load_settings(config_dir).providers["anthropic"]["timeout_s"] == 899


def test_network_is_blocked_outside_live_tests():
    import socket
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("api.anthropic.com", 443))


def test_credentials_removed_outside_live_tests():
    import os
    assert "ANTHROPIC_API_KEY" not in os.environ and "LLM_LIVE" not in os.environ
