"""Étape 5 : lots — réservation à l'envoi, remplacement par le coût réel à la collecte, lots oubliés."""
import pytest

from journal import db
from llm import adapter
from llm.guards import BudgetExceeded
from llm.pricing import Usage
from llm.providers.base import ProviderResponse
from llm.providers.fake import FakeProvider
from tests.conftest import edit_yaml, rows, seed_spend

CONV = [{"role": "user", "content": "Analyse la niche bougies"}]
USAGE = Usage(tokens_in=2000, tokens_out=1000)
REAL_ITEM_COST = (2000 * 2 + 1000 * 10) / 1e6 * 0.5     # Sonnet, tarif batch


def fixed_fake():
    return FakeProvider(responder=lambda req: ProviderResponse(text='{"ok": true}', usage=USAGE))


def spend(rt, purpose="prod"):
    return db.month_spend(rt.conn, db.month_key(rt.clock()), purpose)


def test_batch_reserves_then_collect_replaces_with_real_cost(make_runtime):
    rt = make_runtime(provider=fixed_fake())
    h = adapter.call("veille", None, [CONV, CONV], runtime=rt)
    assert h.n_items == 2
    assert spend(rt) == pytest.approx(h.reserved_usd)    # réservation comptée dans le cumul
    (res_row,) = rows(rt, status="reserved")
    assert res_row["batch_id"] == h.batch_id and res_row["mode"] == "batch"

    assert adapter.collect_batch(h.batch_id, runtime=rt) is None   # pas encore terminé
    rt.fake.finish(h.batch_id)
    out = adapter.collect_batch(h.batch_id, runtime=rt)

    assert set(out) == set(h.custom_ids)
    assert all(o.cost_usd == pytest.approx(REAL_ITEM_COST) for o in out.values())
    assert spend(rt) == pytest.approx(2 * REAL_ITEM_COST)          # cumul exact après collecte
    assert rows(rt, call_id=h.call_id)[0]["status"] == "released"
    assert len(rows(rt, batch_id=h.batch_id, status="ok")) == 2


def test_batch_collect_validates_schema(make_runtime):
    rt = make_runtime(provider=fixed_fake())
    h = adapter.call("veille", None, [CONV], runtime=rt)
    rt.fake.finish(h.batch_id)
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    out = adapter.collect_batch(h.batch_id, schema=schema, runtime=rt)
    assert next(iter(out.values())).content == {"ok": True}


def test_batch_collected_twice_is_rejected(make_runtime):
    rt = make_runtime(provider=fixed_fake())
    h = adapter.call("veille", None, [CONV], runtime=rt)
    rt.fake.finish(h.batch_id)
    adapter.collect_batch(h.batch_id, runtime=rt)
    with pytest.raises(LookupError):
        adapter.collect_batch(h.batch_id, runtime=rt)


def test_batch_over_cap_not_sent(rt):
    seed_spend(rt, 59.99)
    with pytest.raises(BudgetExceeded):
        adapter.call("veille", None, [CONV] * 3, runtime=rt)
    assert rt.fake.batches == {}                        # rien n'est envoyé
    assert rows(rt, status="refused")
    assert any("refusé" in m for m in rt.notifier.messages)


def test_second_batch_refused_when_reservations_exceed_cap(config_dir, make_runtime):
    # 1 élément veille : réservation ≈ 8192 x 10 x 0,5 / 1e6 ≈ 0,041 $. Plafond 0,06 $ : un seul lot passe.
    edit_yaml(config_dir / "agents.yaml", lambda d: d["budgets"]["prod"].update(global_monthly=0.06))
    rt = make_runtime()
    h1 = adapter.call("veille", None, [CONV], runtime=rt)
    assert 0.03 < h1.reserved_usd < 0.06
    with pytest.raises(BudgetExceeded):
        adapter.call("veille", None, [CONV], runtime=rt)
    assert list(rt.fake.batches) == [h1.batch_id]


def test_batch_per_shop_reservation(rt):
    seed_spend(rt, 44.99, shop_id="shop-a")
    with pytest.raises(BudgetExceeded) as exc:
        adapter.call("boutique", "shop-a", [CONV] * 2, runtime=rt)
    assert exc.value.cap.key == "prod:shop:shop-a"


def test_stale_batch_alert_after_24h(rt):
    h = adapter.call("veille", None, [CONV], runtime=rt)
    rt.clock.advance(hours=23)
    assert adapter.check_stale_batches(runtime=rt) == []
    rt.clock.advance(hours=2)
    assert adapter.check_stale_batches(runtime=rt) == [h.batch_id]
    assert adapter.check_stale_batches(runtime=rt) == [h.batch_id]   # toujours en retard...
    assert sum(h.batch_id in m for m in rt.notifier.messages) == 1  # ...mais une seule alerte


def test_batch_requires_list_of_conversations(rt):
    with pytest.raises(ValueError):
        adapter.call("veille", None, CONV, runtime=rt)           # une seule conversation
    with pytest.raises(ValueError):
        adapter.call("veille", None, [], runtime=rt)


def test_dry_run_batch(make_runtime):
    rt = make_runtime(live=False)
    h = adapter.call("contenu", "shop-a", [CONV], runtime=rt)
    assert spend(rt) == 0                                         # réservation simulée : hors cumul
    rt.dry_run_provider.finish(h.batch_id)
    out = adapter.collect_batch(h.batch_id, runtime=rt)
    assert len(out) == 1 and rt.fake.batches == {}
