"""Session 2, point 5 : outil manuel des réservations de lot perdues (list, attach, release)."""
import datetime as dt
import logging

import pytest

from journal import db
from journal.release_reservation import main
from llm import adapter
from tests.conftest import NOW, rows
from tests.fakes import fake_anthropic_key

MSG = [{"role": "user", "content": "Analyse la niche bougies"}]
STALE_H = 24


def _lost_reservation(rt, monkeypatch):
    """Plantage simulé entre l'envoi du lot et l'écriture de l'identifiant fournisseur."""
    real_update = db.update_call

    def crash_on_batch_id(conn, call_id, **fields):
        if "batch_id" in fields:
            raise KeyboardInterrupt("processus tué")
        real_update(conn, call_id, **fields)
    monkeypatch.setattr(db, "update_call", crash_on_batch_id)
    with pytest.raises(KeyboardInterrupt):
        adapter.call("veille", None, [MSG], runtime=rt)
    monkeypatch.undo()
    (r,) = rows(rt, status="reserved")
    return r


def _run(rt, *argv, hours=25):
    return main(list(argv), conn=rt.conn, min_age_hours=STALE_H, now=NOW + dt.timedelta(hours=hours))


def _spend(rt):
    return db.month_spend(rt.conn, "2026-11", "prod")


# ---------------------------------------------------------------- list

def test_list_shows_old_provisional_reservations(rt, monkeypatch, capsys):
    r = _lost_reservation(rt, monkeypatch)
    assert _run(rt, "list", hours=23) == 0
    assert "Aucune" in capsys.readouterr().out                     # trop récente
    assert _run(rt, "list") == 0
    out = capsys.readouterr().out
    assert r["call_id"] in out and "claude-sonnet-5-5" in out and "25.0 h" in out
    assert f"{r['cost_usd']:.4f} $" in out


def test_list_ignores_reservations_with_provider_id(rt, capsys):
    adapter.call("veille", None, [MSG], runtime=rt)                 # lot normal, identifiant réel écrit
    assert _run(rt, "list", hours=48) == 0
    assert "Aucune" in capsys.readouterr().out


# ---------------------------------------------------------------- release

def test_release_refused_without_confirm(rt, monkeypatch, capsys):
    r = _lost_reservation(rt, monkeypatch)
    before = _spend(rt)
    assert _run(rt, "release", r["call_id"], "--reason", "lot absent de la console") == 1
    assert "--confirm" in capsys.readouterr().err
    assert rows(rt, call_id=r["call_id"])[0]["status"] == "reserved" and _spend(rt) == before


def test_release_refused_if_too_recent(rt, monkeypatch, capsys):
    r = _lost_reservation(rt, monkeypatch)
    assert _run(rt, "release", r["call_id"], "--reason", "x", "--confirm", hours=2) == 1
    assert "âge minimal" in capsys.readouterr().err
    assert rows(rt, call_id=r["call_id"])[0]["status"] == "reserved"


def test_release_refused_if_not_reserved_or_not_provisional(rt, capsys):
    ok = adapter.call("sav", "shop-a", MSG, runtime=rt)            # ligne `ok`
    assert _run(rt, "release", ok.call_id, "--reason", "x", "--confirm") == 1
    assert "pas 'reserved'" in capsys.readouterr().err
    h = adapter.call("veille", None, [MSG], runtime=rt)            # réservée, identifiant fournisseur réel
    assert _run(rt, "release", h.call_id, "--reason", "x", "--confirm") == 1
    assert "identifiant fournisseur" in capsys.readouterr().err
    assert rows(rt, call_id=h.call_id)[0]["status"] == "reserved"
    assert _run(rt, "release", "inconnu", "--reason", "x", "--confirm") == 1


def test_release_frees_the_reservation_and_logs_without_client_data(rt, monkeypatch, caplog, capsys):
    r = _lost_reservation(rt, monkeypatch)
    assert _spend(rt) == pytest.approx(r["cost_usd"])
    key = fake_anthropic_key()
    reason = f"lot absent de la console, vérifié par jean.dupont@example.com avec {key}"
    with caplog.at_level(logging.WARNING):
        assert _run(rt, "release", r["call_id"], "--reason", reason, "--confirm") == 0
    row = rows(rt, call_id=r["call_id"])[0]
    assert row["status"] == "abandoned" and row["cost_usd"] == 0
    assert _spend(rt) == 0                                          # plafond libéré
    assert "libération manuelle le 2026-11-09T13:00:00" in row["error"] and "lot absent" in row["error"]
    for text in (row["error"], caplog.text, capsys.readouterr().out):
        assert "jean.dupont@example.com" not in text and key not in text
    assert "libérée manuellement" in caplog.text
    assert _run(rt, "release", r["call_id"], "--reason", "x", "--confirm") == 1   # pas deux fois


def test_released_batch_no_longer_reported_as_stale(rt, monkeypatch):
    r = _lost_reservation(rt, monkeypatch)
    _run(rt, "release", r["call_id"], "--reason", "jamais envoyé", "--confirm")
    rt.clock.advance(hours=30)
    assert adapter.check_stale_batches(runtime=rt) == []


# ---------------------------------------------------------------- attach

def test_attach_then_collect_replaces_reservation_with_real_cost(rt, monkeypatch, capsys):
    r = _lost_reservation(rt, monkeypatch)
    (provider_id,) = rt.fake.batches                               # identifiant lu dans la console fournisseur
    assert _run(rt, "attach", r["call_id"], provider_id) == 0
    row = rows(rt, call_id=r["call_id"])[0]
    assert row["batch_id"] == provider_id and row["status"] == "reserved" and "rattaché" in row["error"]
    rt.fake.finish(provider_id)
    out = adapter.collect_batch(provider_id, runtime=rt)
    assert len(out) == 1
    assert rows(rt, call_id=r["call_id"])[0]["status"] == "released"
    assert _spend(rt) == pytest.approx(next(iter(out.values())).cost_usd)


def test_attach_refusals(rt, monkeypatch, capsys):
    r = _lost_reservation(rt, monkeypatch)
    assert _run(rt, "attach", r["call_id"], "pending-abc") == 1                 # identifiant provisoire
    assert _run(rt, "attach", r["call_id"], "msgbatch_x", hours=1) == 1         # trop récente
    h = adapter.call("veille", None, [MSG], runtime=rt)
    assert _run(rt, "attach", r["call_id"], h.batch_id) == 1                    # déjà rattaché ailleurs
    assert "déjà rattaché" in capsys.readouterr().err
    assert _run(rt, "attach", h.call_id, "msgbatch_y", hours=48) == 1           # ligne non provisoire
    assert rows(rt, call_id=r["call_id"])[0]["batch_id"].startswith("pending-")


def test_migration_005_documents_manual_release(rt):
    desc = rt.conn.execute("SELECT description FROM llm_call_statuses WHERE status = 'abandoned'").fetchone()[0]
    assert "release_reservation" in desc
