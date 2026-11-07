"""Dépendances de l'adaptateur (config, tarifs, journal, alertes, fournisseurs, horloge).

En production, `get_runtime()` les construit depuis l'environnement. Les tests passent un Runtime explicite.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from alerts.telegram import Notifier, TelegramNotifier
from journal import db
from journal.redact import install_redaction
from llm.config import PriceTable, Settings, load_prices, load_settings
from llm.providers import create_provider
from llm.providers.base import Provider
from llm.providers.fake import FakeProvider


@dataclass
class Runtime:
    settings: Settings
    prices: PriceTable
    conn: sqlite3.Connection
    notifier: Notifier
    live: bool = False                       # dry-run par défaut
    provider_factory: Callable[[str], Provider] = create_provider
    clock: Callable[[], dt.datetime] = db.utc_now
    sleep: Callable[[float], None] = time.sleep
    dry_run_provider: FakeProvider = field(default_factory=FakeProvider)
    _providers: dict[str, Provider] = field(default_factory=dict, repr=False)

    def provider_for(self, name: str, live: bool | None = None) -> Provider:
        """Fournisseur réel si live, sinon le fournisseur simulé (aucun appel réseau)."""
        if not (self.live if live is None else live):
            return self.dry_run_provider
        if name not in self._providers:
            self._providers[name] = self.provider_factory(name)
        return self._providers[name]

    @classmethod
    def from_env(cls) -> "Runtime":
        install_redaction()
        return cls(
            settings=load_settings(),
            prices=load_prices(),
            conn=db.connect(),
            notifier=TelegramNotifier.from_env(),
            live=os.environ.get("LLM_LIVE") == "1",
        )


_default: Runtime | None = None


def get_runtime() -> Runtime:
    global _default
    if _default is None:
        _default = Runtime.from_env()
    return _default
