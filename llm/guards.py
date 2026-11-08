"""Garde-fous déterministes, hors LLM : outils autorisés, plafonds mensuels, alertes de seuil.

Ces règles ne lisent que la config et le journal. Aucune n'est contournable par un argument d'appel.
"""
from __future__ import annotations

import datetime as dt
import re
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


class SchemaNotSupported(GuardError):
    """Schéma de sortie hors du sous-ensemble JSON Schema des sorties structurées : refusé avant l'appel."""


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


# ---------------------------------------------------------------- schéma de sortie
# Sous-ensemble JSON Schema accepté par les sorties structurées (doc officielle « Structured outputs »,
# sections « JSON Schema limitations » et « Schema complexity limits »). Liste blanche : tout mot-clé absent
# est refusé, plutôt que de laisser l'API répondre 400. `description` et `title` sont des annotations sans
# effet sur la grammaire, acceptées.
SCHEMA_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
SCHEMA_FORMATS = frozenset({"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6",
                            "uuid"})
SCHEMA_KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum", "const",
                             "anyOf", "allOf", "$ref", "$defs", "definitions", "default", "format", "pattern",
                             "minItems", "description", "title"})
MAX_OPTIONAL_PARAMS = 24      # paramètres hors `required`, tous objets confondus
MAX_UNION_PARAMS = 16         # propriétés en anyOf ou à type multiple
MAX_REGEX_REPEAT = 100        # borne d'un quantificateur {n,m} (« grandes plages » non supportées)
_REGEX_UNSUPPORTED = (
    (re.compile(r"\\[1-9]"), "référence arrière"),
    (re.compile(r"\(\?<?[=!]"), "assertion avant/arrière"),
    (re.compile(r"\\[bB]"), r"limite de mot \b"),
)
_REGEX_REPEAT = re.compile(r"\{(\d+)(?:,(\d*))?\}")


class _SchemaWalk:
    def __init__(self, root: dict[str, Any]):
        self.root = root
        self.errors: list[str] = []
        self.optional = 0
        self.unions = 0
        self.refs: dict[str, set[str]] = {}      # propriétaire (« # » ou définition) -> références

    def err(self, path: str, msg: str) -> None:
        self.errors.append(f"{path} : {msg}")

    def walk(self, node: Any, path: str, owner: str) -> None:
        if not isinstance(node, dict):
            self.err(path, "sous-schéma qui n'est pas un objet")
            return
        for key in node:
            if key not in SCHEMA_KEYWORDS:
                self.err(path, f"mot-clé non supporté {key!r}")
        types = node.get("type")
        type_list = types if isinstance(types, list) else [types] if types is not None else []
        for t in type_list:
            if t not in SCHEMA_TYPES:
                self.err(path, f"type non supporté {t!r}")
        if ("object" in type_list or "properties" in node or "additionalProperties" in node) \
                and node.get("additionalProperties") is not False:
            self.err(path, "objet sans additionalProperties: false")
        props = node.get("properties", {})
        if not isinstance(props, dict):
            self.err(path, "properties doit être un objet")
            props = {}
        required = node.get("required", [])
        if not isinstance(required, list) or any(r not in props for r in required):
            self.err(path, "required doit lister des propriétés déclarées")
            required = []
        for name, sub in props.items():
            if name not in required:
                self.optional += 1
            if isinstance(sub, dict) and ("anyOf" in sub or isinstance(sub.get("type"), list)):
                self.unions += 1
            self.walk(sub, f"{path}/properties/{name}", owner)
        if "items" in node:
            self.walk(node["items"], f"{path}/items", owner)
        for key in ("anyOf", "allOf"):
            if key in node:
                subs = node[key]
                if not isinstance(subs, list) or not subs:
                    self.err(path, f"{key} doit être une liste non vide")
                    continue
                for i, sub in enumerate(subs):
                    if key == "allOf" and isinstance(sub, dict) and "$ref" in sub:
                        self.err(path, "allOf avec $ref non supporté")
                    self.walk(sub, f"{path}/{key}/{i}", owner)
        for key in ("$defs", "definitions"):
            defs = node.get(key, {})
            if not isinstance(defs, dict):
                self.err(path, f"{key} doit être un objet")
                continue
            for name, sub in defs.items():
                self.walk(sub, f"{path}/{key}/{name}", f"#/{key}/{name}")
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#"):
                self.err(path, "$ref externe non supporté")
            else:
                self.refs.setdefault(owner, set()).add(ref)
        if "enum" in node:
            values = node["enum"]
            if not isinstance(values, list) or not values or \
                    any(v is not None and not isinstance(v, (str, int, float, bool)) for v in values):
                self.err(path, "enum : valeurs simples uniquement (chaînes, nombres, booléens, null)")
        if "format" in node and node["format"] not in SCHEMA_FORMATS:
            self.err(path, f"format non supporté {node['format']!r}")
        if "minItems" in node and (isinstance(node["minItems"], bool) or node["minItems"] not in (0, 1)):
            self.err(path, "minItems : seules les valeurs 0 et 1 sont supportées")
        if "pattern" in node:
            self.check_pattern(node["pattern"], path)

    def check_pattern(self, pattern: Any, path: str) -> None:
        if not isinstance(pattern, str):
            self.err(path, "pattern doit être une chaîne")
            return
        try:
            re.compile(pattern)
        except re.error:
            self.err(path, "pattern : expression régulière invalide")
            return
        for rx, label in _REGEX_UNSUPPORTED:
            if rx.search(pattern):
                self.err(path, f"pattern : {label} non supportée")
        for m in _REGEX_REPEAT.finditer(pattern):
            if max(int(m.group(1)), int(m.group(2) or 0)) > MAX_REGEX_REPEAT:
                self.err(path, f"pattern : quantificateur {m.group(0)} trop grand (> {MAX_REGEX_REPEAT})")

    def check_refs(self) -> None:
        """Références résolubles dans le schéma, et aucun cycle (schémas récursifs non supportés)."""
        for owner, refs in self.refs.items():
            for ref in sorted(refs):
                if ref == "#":
                    self.err(owner, "schéma récursif (référence à la racine)")
                elif self._resolve(ref) is None:
                    self.err(owner, f"$ref introuvable {ref!r}")
        state: dict[str, int] = {}                # 1 = en cours, 2 = terminé

        def cyclic(node: str) -> bool:
            if state.get(node) == 1:
                return True
            if state.get(node) == 2:
                return False
            state[node] = 1
            found = any(cyclic(r) for r in self.refs.get(node, ()) if r != "#")
            state[node] = 2
            return found
        if any(cyclic(owner) for owner in list(self.refs)):
            self.errors.append("schéma récursif ($ref en cycle) non supporté")

    def _resolve(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            return None
        node: Any = self.root
        for part in ref[2:].split("/"):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node


def check_schema(schema: dict[str, Any] | None) -> None:
    """Refuse un schéma de sortie hors du sous-ensemble supporté. Aucun appel n'est fait : coût 0."""
    if schema is None:
        return
    w = _SchemaWalk(schema)
    w.walk(schema, "#", "#")
    w.check_refs()
    if w.optional > MAX_OPTIONAL_PARAMS:
        w.errors.append(f"{w.optional} paramètres optionnels (maximum {MAX_OPTIONAL_PARAMS})")
    if w.unions > MAX_UNION_PARAMS:
        w.errors.append(f"{w.unions} paramètres à type union (maximum {MAX_UNION_PARAMS})")
    if w.errors:
        more = f" (+{len(w.errors) - 5} autres)" if len(w.errors) > 5 else ""
        raise SchemaNotSupported("schéma de sortie non supporté par les sorties structurées : "
                                 + " ; ".join(w.errors[:5]) + more)


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
