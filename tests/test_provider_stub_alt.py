"""Session 2, point 3 : second fournisseur (stub sans réseau).

Passer un agent à ce fournisseur ne demande que config/agents.yaml (+ son entrée dans le registre, déjà faite) :
aucun fichier de code n'est modifié par le test, et adapter.py / guards.py n'importent aucun fournisseur
(contrôle AST dans test_completion_and_search.py).
"""
import pytest

from alerts.telegram import MemoryNotifier
from journal import db
from llm import adapter
from llm.adapter import LLMCallError
from llm.config import load_prices, load_settings
from llm.providers import _FACTORIES, create_provider
from llm.providers.base import ProviderError, ProviderRequest
from llm.providers.stub_alt import StubAltProvider
from llm.runtime import Runtime
from tests.conftest import NOW, Clock, edit_yaml, rows

MSG = [{"role": "user", "content": "Où est ma commande ?"}]
SCHEMA = {"type": "object", "required": ["reponse"], "properties": {"reponse": {"type": "string"}},
          "additionalProperties": False}


def _switch_sav_to_stub(config_dir):
    # La seule modification nécessaire : deux lignes de agents.yaml.
    edit_yaml(config_dir / "agents.yaml",
              lambda d: d["agents"]["sav"].update(provider="stub_alt", model="stub-alt"))


def _live_runtime(config_dir):
    # Fabrique par défaut (registre) : aucun fournisseur injecté par le test.
    return Runtime(settings=load_settings(config_dir), prices=load_prices(config_dir), conn=db.connect(":memory:"),
                   notifier=MemoryNotifier(), live=True, clock=Clock(NOW), sleep=lambda s: None)


def test_stub_registered():
    assert "stub_alt" in _FACTORIES
    assert isinstance(create_provider("stub_alt"), StubAltProvider)


def test_agent_switched_by_config_only(config_dir):
    _switch_sav_to_stub(config_dir)
    rt = _live_runtime(config_dir)
    res = adapter.call("sav", "shop-a", MSG, schema=SCHEMA, purpose="eval", runtime=rt)
    assert res.provider == "stub_alt" and res.model == "stub-alt" and res.content == {"reponse": ""}
    (row,) = rows(rt, call_id=res.call_id)
    assert row["provider"] == "stub_alt" and row["dry_run"] == 0 and row["cost_usd"] == 0
    assert rt.notifier.messages == []              # tarif présent dans prices.yaml : pas d'alerte de repli


def test_stub_refuses_prod_in_live_mode(config_dir):
    _switch_sav_to_stub(config_dir)
    rt = _live_runtime(config_dir)
    with pytest.raises(LLMCallError, match="purpose=prod"):
        adapter.call("sav", "shop-a", MSG, runtime=rt)          # purpose="prod" par défaut
    (row,) = rows(rt)
    assert row["status"] == "error" and row["cost_usd"] == 0
    assert len(rows(rt)) == 1                                   # non retryable : aucune nouvelle tentative


def test_stub_refuses_prod_directly_and_in_batches():
    stub = StubAltProvider()
    req = ProviderRequest(model="stub-alt", messages=MSG, max_tokens=10)
    with pytest.raises(ProviderError) as err:
        stub.complete(req)
    assert err.value.retryable is False
    with pytest.raises(ProviderError):
        stub.submit_batch([("item-0", req)])


def test_stub_batch_in_eval(config_dir):
    edit_yaml(config_dir / "agents.yaml",
              lambda d: d["agents"]["contenu"].update(provider="stub_alt", model="stub-alt"))
    rt = _live_runtime(config_dir)
    h = adapter.call("contenu", None, [MSG, MSG], purpose="eval", runtime=rt)
    out = adapter.collect_batch(h.batch_id, runtime=rt)
    assert {o.content for o in out.values()} == {"[stub-alt] réponse simulée"}


def test_dry_run_never_reaches_stub(config_dir):
    _switch_sav_to_stub(config_dir)
    rt = _live_runtime(config_dir)
    rt.live = False
    res = adapter.call("sav", "shop-a", MSG, runtime=rt)       # prod en dry-run : fournisseur simulé
    assert res.dry_run is True
