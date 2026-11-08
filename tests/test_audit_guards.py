"""Tests ajoutés par la relecture externe de la session 1 : concurrence, réservations orphelines, libération après échec, marge d'estimation."""
import datetime as dt
import sqlite3
import threading

import pytest

from alerts.telegram import MemoryNotifier
from journal import db
from llm import adapter, guards
from llm.config import load_prices, load_settings
from llm.providers.base import ProviderError
from llm.providers.fake import FakeProvider, default_responder
from llm.runtime import Runtime
from tests.conftest import NOW, Clock, edit_yaml, rows

MSG = [{"role": "user", "content": "x"}]


def _rt(config_dir, conn, provider=None):
    return Runtime(settings=load_settings(config_dir), prices=load_prices(config_dir), conn=conn,
                   notifier=MemoryNotifier(), live=True, provider_factory=lambda n: provider or FakeProvider(),
                   clock=Clock(NOW), sleep=lambda s: None)


def test_concurrent_calls_cannot_both_reserve_the_last_dollars(config_dir, tmp_path, monkeypatch):
    # plafond global = ~2 appels "sourcing" estimés à 0,001025 $ : un seul doit passer
    edit_yaml(config_dir / "agents.yaml", lambda d: d["budgets"]["prod"].update(global_monthly=0.002, per_shop_monthly=1))
    path = tmp_path / "j.sqlite"
    db.connect(path).close()                     # migrations faites avant les threads
    barrier = threading.Barrier(2, timeout=1.5)
    orig = guards.check_budget

    def slow(conn, caps, month, amount):
        orig(conn, caps, month, amount)          # lecture du cumul
        try:
            barrier.wait()                       # force les deux lectures avant toute écriture
        except threading.BrokenBarrierError:
            pass
    monkeypatch.setattr(guards, "check_budget", slow)

    outcome = {}
    gate = threading.Event()

    def gated(request):                          # l'appel reste "en vol" : sa réservation doit tenir
        gate.wait(3)
        return default_responder(request)

    def worker(name):
        conn = db.connect(path)
        try:
            adapter.call("sourcing", "shop-a", MSG, runtime=_rt(config_dir, conn, FakeProvider(responder=gated)))
            outcome[name] = "ok"
        except guards.BudgetExceeded:
            outcome[name] = "refused"
        except Exception as exc:                 # noqa: BLE001
            outcome[name] = f"erreur:{type(exc).__name__}:{exc}"
    ts = [threading.Thread(target=worker, args=(n,)) for n in ("t1", "t2")]
    [t.start() for t in ts]; [t.join(15) for t in ts]
    assert sorted(outcome.values()) == ["ok", "refused"]


def test_orphan_pending_row_does_not_block_budget_forever(config_dir):
    conn = db.connect(":memory:")
    rt = _rt(config_dir, conn)
    # crash simulé : une réservation "pending" de 59,99 $ posée il y a 3 h, jamais mise à jour
    db.insert_call(conn, ts=NOW - dt.timedelta(hours=3), agent="sav", provider="anthropic", model="claude-haiku-5-5",
                   mode="sync", purpose="prod", status="pending", cost_usd=59.99, shop_id="shop-a")
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)      # doit passer une fois la réservation expirée


def test_failed_attempt_releases_its_reservation(config_dir):
    conn = db.connect(":memory:")
    rt = _rt(config_dir, conn, FakeProvider(failures=[ProviderError("529", True)]))
    adapter.call("sourcing", "shop-a", MSG, runtime=rt)
    assert [r["cost_usd"] for r in rows(rt, status="error")] == [0]


def test_estimation_margin_is_20_percent_in_repo_config(config_dir):
    assert load_settings(config_dir).input_margin == pytest.approx(0.20)
