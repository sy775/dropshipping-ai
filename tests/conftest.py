"""Fixtures communes : runtime isolé (config copiée, journal en mémoire, fournisseur simulé, horloge fixe)."""
from __future__ import annotations

import datetime as dt
import shutil
import socket
from pathlib import Path

import pytest
import yaml

from alerts.telegram import MemoryNotifier
from journal import db
from llm.config import load_prices, load_settings
from llm.providers.fake import FakeProvider
from llm.runtime import Runtime

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)


_CREDENTIAL_VARS = ("LLM_LIVE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
                    "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE", "ANTHROPIC_FEDERATION_RULE_ID")


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _isolation(request, monkeypatch):
    """Hors tests `live` : aucune clé ni LLM_LIVE dans l'environnement, et tout accès réseau échoue."""
    if request.node.get_closest_marker("live"):
        return
    for var in _CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)

    def blocked(*args, **kwargs):
        raise NetworkBlocked("accès réseau interdit dans les tests (marquer le test `live`)")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


class Clock:
    def __init__(self, now: dt.datetime):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += dt.timedelta(**kw)


@pytest.fixture
def config_dir(tmp_path):
    dst = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, dst)
    return dst


def edit_yaml(path: Path, mutate) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def make_runtime(config_dir):
    """Fabrique un runtime ; live=True avec le fournisseur simulé branché à la place du vrai."""
    def _make(live: bool = True, provider: FakeProvider | None = None, conn=None) -> Runtime:
        provider = provider or FakeProvider()
        sleeps: list[float] = []
        rt = Runtime(
            settings=load_settings(config_dir),
            prices=load_prices(config_dir),
            conn=conn or db.connect(":memory:"),
            notifier=MemoryNotifier(),
            live=live,
            provider_factory=lambda name: provider,
            clock=Clock(NOW),
            sleep=sleeps.append,
        )
        rt.fake = provider       # accès direct pour les assertions
        rt.sleeps = sleeps
        return rt
    return _make


@pytest.fixture
def rt(make_runtime):
    return make_runtime()


def seed_spend(rt: Runtime, amount: float, purpose: str = "prod", shop_id: str | None = None) -> None:
    """Simule une dépense déjà faite ce mois-ci."""
    db.insert_call(rt.conn, ts=rt.clock(), agent="sav", provider="anthropic", model="claude-haiku-5-5",
                   mode="sync", purpose=purpose, status="ok", cost_usd=amount, shop_id=shop_id)


def rows(rt: Runtime, **where):
    sql = "SELECT * FROM llm_calls"
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
    return rt.conn.execute(sql + " ORDER BY ts, rowid", tuple(where.values())).fetchall()
