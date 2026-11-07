"""Garde-fous déterministes, hors LLM : outils autorisés, plafonds mensuels, alertes de seuil.

Ces règles ne lisent que la config et le journal. Aucune n'est contournable par un argument d'appel.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Any

from alerts.telegram import Notifier
from journal import db
from llm.config import AgentConfig, Budgets

# Règle 3 : seul cet agent peut recevoir un outil web. Codé ici, pas dans la config.
WEB_AGENT = "veille"
_WEB_MARKERS = ("web", "google_search", "url_context", "browse", "fetch_url", "http")


class GuardError(Exception):
    pass


class ToolNotAllowed(GuardError):
    pass


@dataclass(frozen=True)
class Cap:
    key: str          # ex. "prod:global", "prod:shop:shop-a", "eval:global"
    limit: float
    purpose: str
    shop_id: str | None = None


class BudgetExceeded(GuardError):
    def __init__(self, cap: Cap, current: float, requested: float):
        self.cap, self.current, self.requested = cap, current, requested
        super().__init__(
            f"plafond {cap.key} : {current:.4f} $ déjà engagés + {requested:.4f} $ estimés "
            f"> {cap.limit:.2f} $"
        )


# ---------------------------------------------------------------- outils

def is_web_tool(tool: dict[str, Any]) -> bool:
    ident = f"{tool.get('type', '')} {tool.get('name', '')}".lower()
    return any(marker in ident for marker in _WEB_MARKERS)


def check_tools(agent: AgentConfig, tools: list[dict[str, Any]] | None) -> None:
    for tool in tools or []:
        if not isinstance(tool, dict):
            raise ToolNotAllowed(f"outil mal formé pour {agent.name}")
        name = tool.get("name") or tool.get("type") or "?"
        # Couche 1 : liste blanche de l'agent (allowed_tools). Vide = aucun outil, y compris pour veille.
        if name not in agent.allowed_tools:
            raise ToolNotAllowed(f"outil {name!r} non autorisé pour l'agent {agent.name} (absent de allowed_tools)")
        # Couche 2 : détection par mots-clés, indépendante de la config.
        if is_web_tool(tool) and agent.name != WEB_AGENT:
            raise ToolNotAllowed(f"outil web {name!r} interdit pour l'agent {agent.name} (réservé à {WEB_AGENT})")


# ---------------------------------------------------------------- plafonds

def caps_for(budgets: Budgets, purpose: str, shop_id: str | None) -> list[Cap]:
    if purpose == "eval":
        return [Cap("eval:global", budgets.eval_global, "eval")]
    caps = [Cap("prod:global", budgets.prod_global, "prod")]
    if shop_id is not None:
        caps.append(Cap(f"prod:shop:{shop_id}", budgets.shop_cap(shop_id), "prod", shop_id))
    return caps


def cap_spend(conn: sqlite3.Connection, cap: Cap, month: str) -> float:
    return db.month_spend(conn, month, cap.purpose, cap.shop_id)


def check_budget(conn: sqlite3.Connection, caps: list[Cap], month: str, amount: float) -> None:
    """Lève BudgetExceeded si le montant ferait dépasser un plafond. À appeler dans une transaction
    qui enregistre aussi la réservation, pour que contrôle et réservation soient atomiques."""
    for cap in caps:
        current = cap_spend(conn, cap, month)
        if current + amount > cap.limit:
            raise BudgetExceeded(cap, current, amount)


def threshold_alerts(conn: sqlite3.Connection, caps: list[Cap], month: str, ratio: float,
                     notifier: Notifier, now: dt.datetime) -> None:
    """Alerte quand un plafond atteint `ratio` : une seule fois par mois et par plafond."""
    for cap in caps:
        current = cap_spend(conn, cap, month)
        if cap.limit > 0 and current >= ratio * cap.limit:
            if db.record_alert_once(conn, f"threshold:{cap.key}:{month}", now):
                notify(notifier, f"Plafond LLM {cap.key} à {int(ratio * 100)} % : "
                                 f"{current:.2f} $ / {cap.limit:.2f} $ ({month}).")


def notify(notifier: Notifier, text: str) -> None:
    """Envoie une alerte sans jamais interrompre l'appelant."""
    try:
        notifier.send(text)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("échec du notificateur")
