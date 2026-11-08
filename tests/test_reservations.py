"""Réservations orphelines (pending expirées), lots à identifiant provisoire, mois de facturation des lots."""
import datetime as dt

import pytest

from journal import db
from llm import adapter
from tests.conftest import NOW, edit_yaml, rows

MSG = [{"role": "user", "content": "x"}]


def _pending(rt, age: dt.timedelta, cost=1.0, shop_id="shop-a"):
    return db.insert_call(rt.conn, ts=rt.clock() - age, agent="sav", provider="anthropic", model="claude-haiku-5-5",
                          mode="sync", purpose="prod", status="pending", cost_usd=cost, shop_id=shop_id)


def _orphan_alerts(rt):
    return [m for m in rt.notifier.messages if "abandonnée" in m]


# ---------------------------------------------------------------- point 1 : pending expirées

def test_pending_older_than_ttl_is_abandoned(rt):
    cid = _pending(rt, dt.timedelta(hours=3), cost=59.99)
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    (r,) = rows(rt, call_id=cid)
    assert r["status"] == "abandoned" and r["cost_usd"] == 0
    assert "expirée" in r["error"]
    assert len(_orphan_alerts(rt)) == 1


def test_recent_pending_is_kept(rt):
    cid = _pending(rt, dt.timedelta(minutes=5), cost=0.5)
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    (r,) = rows(rt, call_id=cid)
    assert r["status"] == "pending" and r["cost_usd"] == 0.5
    assert _orphan_alerts(rt) == []


def test_ttl_read_from_config(config_dir, make_runtime):
    # le délai d'appel doit rester < TTL (session 2) : 60 s < 2 min
    edit_yaml(config_dir / "agents.yaml", lambda d: d.update(reservations={"pending_ttl_minutes": 2},
                                                            providers={"anthropic": {"timeout_s": 60}}))
    rt = make_runtime()
    cid = _pending(rt, dt.timedelta(minutes=5))
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert rows(rt, call_id=cid)[0]["status"] == "abandoned"


def test_ttl_defaults_to_15_minutes(config_dir):
    from llm.config import load_settings
    edit_yaml(config_dir / "agents.yaml", lambda d: d.pop("reservations"))
    assert load_settings(config_dir).pending_ttl_minutes == 15


def test_orphan_alert_once_per_day(rt):
    _pending(rt, dt.timedelta(hours=3))
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    _pending(rt, dt.timedelta(hours=1))
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert len(_orphan_alerts(rt)) == 1                      # même jour : une seule alerte
    assert len(rows(rt, status="abandoned")) == 2            # mais les deux lignes sont libérées
    rt.clock.advance(days=1)
    _pending(rt, dt.timedelta(hours=1))
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert len(_orphan_alerts(rt)) == 2                      # nouveau jour : nouvelle alerte


def test_batch_reservation_also_expires_orphans(rt):
    cid = _pending(rt, dt.timedelta(hours=3), cost=59.99, shop_id=None)
    adapter.call("veille", None, [MSG], runtime=rt)
    assert rows(rt, call_id=cid)[0]["status"] == "abandoned"


def test_abandoned_is_a_documented_status(rt):
    statuses = {r[0] for r in rt.conn.execute("SELECT status FROM llm_call_statuses")}
    assert "abandoned" in statuses


# ---------------------------------------------------------------- point 2 : lot à identifiant perdu

def test_batch_reservation_carries_provisional_id_then_provider_id(rt):
    h = adapter.call("veille", None, [MSG], runtime=rt)
    (r,) = rows(rt, call_id=h.call_id)
    assert r["batch_id"] == h.batch_id and not h.batch_id.startswith(adapter.PROVISIONAL_BATCH_PREFIX)


def test_crash_between_submit_and_id_write_alerts_after_24h(rt, monkeypatch):
    real_update = db.update_call

    def crash_on_batch_id(conn, call_id, **fields):
        if "batch_id" in fields:
            raise KeyboardInterrupt("processus tué")          # plantage simulé, après l'envoi
        real_update(conn, call_id, **fields)
    monkeypatch.setattr(db, "update_call", crash_on_batch_id)
    with pytest.raises(KeyboardInterrupt):
        adapter.call("veille", None, [MSG], runtime=rt)
    monkeypatch.undo()

    assert len(rt.fake.batches) == 1                          # le lot est bien parti chez le fournisseur
    (r,) = rows(rt, status="reserved")
    assert r["batch_id"] == f"pending-{r['call_id']}"         # identifiant provisoire, dans la même transaction
    assert r["cost_usd"] > 0                                  # la réservation reste comptée

    rt.clock.advance(hours=23)
    assert adapter.check_stale_batches(runtime=rt) == []
    rt.clock.advance(hours=2)
    assert adapter.check_stale_batches(runtime=rt) == [r["batch_id"]]
    adapter.check_stale_batches(runtime=rt)
    alerts = [m for m in rt.notifier.messages if r["call_id"] in m]
    assert len(alerts) == 1 and "sans identifiant fournisseur" in alerts[0]


def test_legacy_reserved_row_without_batch_id_is_reported(rt):
    cid = db.insert_call(rt.conn, ts=NOW - dt.timedelta(hours=30), agent="veille", provider="anthropic",
                         model="claude-sonnet-5-5", mode="batch", purpose="prod", status="reserved", cost_usd=0.04)
    assert adapter.check_stale_batches(runtime=rt) == [f"pending-{cid}"]


# ---------------------------------------------------------------- point 3 : lot à cheval sur deux mois

def test_batch_sent_on_30th_collected_on_2nd_is_billed_to_send_month(make_runtime):
    from llm.pricing import Usage
    from llm.providers.base import ProviderResponse
    from llm.providers.fake import FakeProvider
    usage = Usage(tokens_in=2000, tokens_out=1000)
    rt = make_runtime(provider=FakeProvider(responder=lambda req: ProviderResponse(text="ok", usage=usage)))
    rt.clock.now = dt.datetime(2026, 11, 30, 22, 0, tzinfo=dt.timezone.utc)
    h = adapter.call("veille", None, [MSG, MSG], runtime=rt)
    assert rows(rt, call_id=h.call_id)[0]["billing_month"] == "2026-11"

    rt.clock.now = dt.datetime(2026, 12, 2, 3, 0, tzinfo=dt.timezone.utc)
    rt.fake.finish(h.batch_id)
    out = adapter.collect_batch(h.batch_id, runtime=rt)

    real = sum(o.cost_usd for o in out.values())
    assert real == pytest.approx(2 * (2000 * 2 + 1000 * 10) / 1e6 * 0.5)
    items = rows(rt, batch_id=h.batch_id, status="ok")
    assert all(r["ts"].startswith("2026-12-02") and r["billing_month"] == "2026-11" for r in items)
    assert db.month_spend(rt.conn, "2026-11", "prod") == pytest.approx(real)   # réservation remplacée par le réel
    assert db.month_spend(rt.conn, "2026-12", "prod") == 0
    view = {r["month"]: r["cost_usd"] for r in rt.conn.execute(
        "SELECT month, SUM(cost_usd) AS cost_usd FROM v_llm_cost_monthly WHERE dry_run = 0 GROUP BY month")}
    assert view == {"2026-11": pytest.approx(real)}


def test_sync_call_billing_month_is_call_month(rt):
    res = adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert rows(rt, call_id=res.call_id)[0]["billing_month"] == NOW.strftime("%Y-%m")


def test_migration_003_backfills_billing_month(tmp_path):
    import shutil
    mig = tmp_path / "mig"
    mig.mkdir()
    for f in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if f.stem < "003":
            shutil.copy(f, mig / f.name)
    conn = db.connect(tmp_path / "j.sqlite", migrations_dir=mig)
    conn.execute("INSERT INTO llm_calls (call_id, ts, agent, provider, model, mode, purpose, status, cost_usd) "
                 "VALUES ('old', '2026-10-15T08:00:00+00:00', 'sav', 'anthropic', 'm', 'sync', 'prod', 'ok', 2.5)")
    conn.close()
    conn = db.connect(tmp_path / "j.sqlite")                  # applique 003 sur une base existante
    assert conn.execute("SELECT billing_month FROM llm_calls WHERE call_id = 'old'").fetchone()[0] == "2026-10"
    assert db.month_spend(conn, "2026-10", "prod") == 2.5
