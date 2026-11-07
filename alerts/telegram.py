"""Alertes Telegram. Sans jeton configuré, l'alerte est seulement écrite dans les logs.

Une alerte qui échoue n'interrompt jamais l'appelant : elle est journalisée en avertissement.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request
from typing import Callable, Protocol

from journal.redact import redact

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class TelegramNotifier:
    def __init__(self, token: str | None, chat_id: str | None, timeout: float = 10,
                 opener: Callable = urllib.request.urlopen):
        self._token = token or None
        self._chat_id = chat_id or None
        self._timeout = timeout
        self._opener = opener

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        return cls(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))

    def send(self, text: str) -> None:
        text = redact(text)
        if not (self._token and self._chat_id):
            log.warning("[ALERTE, Telegram non configuré] %s", text)
            return
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            data=urllib.parse.urlencode({"chat_id": self._chat_id, "text": text}).encode(),
            method="POST",
        )
        try:
            with self._opener(req, timeout=self._timeout) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001 - une alerte ne doit jamais casser l'appelant
            # Le message d'erreur peut contenir l'URL, donc le jeton : on masque.
            safe = redact(str(exc)).replace(self._token, "[SECRET]")
            log.warning("échec d'envoi de l'alerte Telegram (%s) : %s", safe, text)


class MemoryNotifier:
    """Notificateur en mémoire, pour les tests et le dry-run."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(redact(text))
