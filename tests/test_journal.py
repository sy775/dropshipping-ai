"""Étape 2 : journal SQLite, migrations rejouables, cumuls mensuels, vue d'agrégation."""
import datetime as dt
import shutil

from journal import db

TS_NOV = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
TS_OCT = dt.datetime(2026, 10, 31, 23, 59, tzinfo=dt.timezone.utc)

EXPECTED_COLUMNS = {
    "call_id", "ts", "shop_id", "agent", "provider", "model", "mode", "purpose",
    "tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write",
    "tokens_in_estimated", "tokens_out_estimated", "cost_estimated_usd", "cost_usd",
    "latency_ms", "status", "error", "attempt", "batch_id", "dry_run",
}


def _row(conn, ts=TS_NOV, **kw):
    base = dict(ts=ts, agent="sav", provider="anthropic", model="claude-haiku-5-5", mode="sync",
                purpose="prod", status="ok", cost_usd=1.0)
    base.update(kw)
    return db.insert_call(conn, **base)


def test_migrations_are_replayable(tmp_path):
    path = tmp_path / "j.sqlite"
    c1 = db.connect(path)
    c1.close()
    c2 = db.connect(path)  # rejoue : ne doit rien réappliquer ni échouer
    versions = [r[0] for r in c2.execute("SELECT version FROM schema_version")]
    assert versions == ["001_llm_calls"]
    assert db.apply_migrations(c2) == []


def test_new_migration_applied_once(tmp_path):
    mig = tmp_path / "migrations"
    shutil.copytree(db.MIGRATIONS_DIR, mig)
    (mig / "002_extra.sql").write_text("CREATE TABLE extra (x INTEGER);", encoding="utf-8")
    conn = db.connect(tmp_path / "j.sqlite", migrations_dir=mig)
    assert db.apply_migrations(conn, mig) == []
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 2


def test_table_holds_only_metrics():
    conn = db.connect(":memory:")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(llm_calls)")}
    assert cols == EXPECTED_COLUMNS  # pas de colonne de prompt ni de contenu


def test_insert_and_update():
    conn = db.connect(":memory:")
    cid = _row(conn, status="pending", cost_usd=0.5)
    db.update_call(conn, cid, status="ok", cost_usd=0.2, tokens_in=10)
    r = conn.execute("SELECT * FROM llm_calls WHERE call_id=?", (cid,)).fetchone()
    assert (r["status"], r["cost_usd"], r["tokens_in"]) == ("ok", 0.2, 10)


def test_month_spend_filters():
    conn = db.connect(":memory:")
    _row(conn, shop_id="a", cost_usd=1.0)
    _row(conn, shop_id="b", cost_usd=2.0)
    _row(conn, shop_id=None, cost_usd=4.0)
    _row(conn, shop_id="a", cost_usd=8.0, purpose="eval")
    _row(conn, shop_id="a", cost_usd=16.0, ts=TS_OCT)       # autre mois
    _row(conn, shop_id="a", cost_usd=32.0, dry_run=1)       # simulé : hors cumul
    assert db.month_spend(conn, "2026-11", "prod") == 7.0
    assert db.month_spend(conn, "2026-11", "prod", shop_id="a") == 1.0
    assert db.month_spend(conn, "2026-11", "eval") == 8.0
    assert db.month_spend(conn, "2026-10", "prod") == 16.0


def test_monthly_view():
    conn = db.connect(":memory:")
    _row(conn, shop_id="a", agent="sav", cost_usd=1.0)
    _row(conn, shop_id="a", agent="sav", cost_usd=2.0)
    _row(conn, shop_id="a", agent="sourcing", cost_usd=4.0)
    rows = conn.execute(
        "SELECT agent, cost_usd, n_calls FROM v_llm_cost_monthly "
        "WHERE month='2026-11' AND shop_id='a' ORDER BY agent").fetchall()
    assert [tuple(r) for r in rows] == [("sav", 3.0, 2), ("sourcing", 4.0, 1)]


def test_alert_dedup():
    conn = db.connect(":memory:")
    assert db.record_alert_once(conn, "k1", TS_NOV) is True
    assert db.record_alert_once(conn, "k1", TS_NOV) is False
    assert db.record_alert_once(conn, "k2", TS_NOV) is True


def test_default_path_is_portable(monkeypatch, tmp_path):
    monkeypatch.setenv("JOURNAL_DB_PATH", str(tmp_path / "sub" / "j.sqlite"))
    assert db.default_db_path() == tmp_path / "sub" / "j.sqlite"
    monkeypatch.delenv("JOURNAL_DB_PATH")
    assert db.default_db_path().parts[-2:] == ("data", "journal.sqlite")


def test_failed_migration_rolls_back(tmp_path):
    import pytest
    import sqlite3
    mig = tmp_path / "migrations"
    shutil.copytree(db.MIGRATIONS_DIR, mig)
    (mig / "002_broken.sql").write_text("CREATE TABLE ok_t (x INTEGER); SELECT * FROM absente;", encoding="utf-8")
    with pytest.raises(sqlite3.Error):
        db.connect(tmp_path / "j.sqlite", migrations_dir=mig)
    conn = db.connect(tmp_path / "j.sqlite")  # sans la migration cassée
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='ok_t'").fetchone() is None
    assert [r[0] for r in conn.execute("SELECT version FROM schema_version")] == ["001_llm_calls"]
