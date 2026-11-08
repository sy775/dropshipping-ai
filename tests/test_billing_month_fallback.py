"""Point 0 : une ligne sans billing_month (insérée en SQL brut) est comptée dans le plafond."""
import pytest

from journal import db
from llm import adapter, guards
from llm.guards import BudgetExceeded
from tests.conftest import NOW, edit_yaml

MSG = [{"role": "user", "content": "x"}]


def _raw_insert(conn, cost, ts=NOW.isoformat()):
    conn.execute("INSERT INTO llm_calls (call_id, ts, agent, provider, model, mode, purpose, status, cost_usd, "
                 "dry_run, billing_month) VALUES ('raw', ?, 'sav', 'anthropic', 'claude-haiku-5-5', 'sync', "
                 "'prod', 'ok', ?, 0, NULL)", (ts, cost))


def test_month_spend_counts_row_without_billing_month(rt):
    _raw_insert(rt.conn, 3.5)
    assert db.month_spend(rt.conn, "2026-11", "prod") == pytest.approx(3.5)
    assert db.month_spend(rt.conn, "2026-10", "prod") == 0


def test_view_and_month_spend_agree(rt):
    _raw_insert(rt.conn, 2.0)
    view = rt.conn.execute("SELECT SUM(cost_usd) FROM v_llm_cost_monthly WHERE month = '2026-11' "
                           "AND purpose = 'prod' AND dry_run = 0").fetchone()[0]
    assert db.month_spend(rt.conn, "2026-11", "prod") == pytest.approx(view)


def test_raw_row_without_billing_month_blocks_call(rt, config_dir):
    _raw_insert(rt.conn, 60.0)          # plafond global prod = 60 $
    caps = guards.caps_for(rt.settings.budgets, "prod", None)
    with pytest.raises(BudgetExceeded):
        guards.check_budget(rt.conn, caps, "2026-11", 0.01)
    with pytest.raises(BudgetExceeded):
        adapter.call("sav", None, MSG, runtime=rt)
