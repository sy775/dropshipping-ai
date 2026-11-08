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
    edit_yaml(config_dir / "agents.yaml", lambda d: d.update(reservations={"pending_ttl_minutes": 2}))
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
