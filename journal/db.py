"""Journal SQLite : connexion, migrations rejouables, écriture des appels LLM, cumuls mensuels."""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

CALL_COLUMNS = (
    "call_id", "ts", "shop_id", "agent", "provider", "model", "mode", "purpose",
    "tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write",
    "tokens_in_estimated", "tokens_out_estimated", "cost_estimated_usd", "cost_usd",
    "latency_ms", "status", "error", "attempt", "batch_id", "dry_run",
    "billing_month",      # migration 003 : mois d'imputation (mois d'envoi pour les lignes d'un lot)
    "stop_reason", "web_search_requests",   # migration 004
)


def default_db_path() -> Path:
    env = os.environ.get("JOURNAL_DB_PATH")
    return Path(env) if env else REPO_ROOT / "data" / "journal.sqlite"


def connect(path: Path | str | None = None, migrations_dir: Path | None = None) -> sqlite3.Connection:
    """Ouvre le journal (créé au besoin) et applique les migrations manquantes."""
    target = default_db_path() if path is None else path
    if str(target) != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None : les transactions sont explicites (BEGIN IMMEDIATE), voir transaction().
    conn = sqlite3.connect(str(target), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    if str(target) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    apply_migrations(conn, migrations_dir or MIGRATIONS_DIR)
    return conn


def apply_migrations(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> list[str]:
    """Applique, dans l'ordre, les fichiers NNN_*.sql non encore enregistrés. Renvoie ceux appliqués."""
    migrations_dir = migrations_dir or MIGRATIONS_DIR
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    applied = []
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        version = sql_file.stem
        if version in done:
            continue
        script = sql_file.read_text(encoding="utf-8")
        now = utc_now().isoformat()
        # executescript valide toute transaction en cours : le script et son enregistrement
        # sont regroupés dans une même transaction explicite.
        try:
            conn.executescript(
                "BEGIN;\n" + script + "\n;"
                f"INSERT INTO schema_version (version, applied_at) VALUES ('{version}', '{now}');\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def month_key(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m")


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transaction à verrou d'écriture immédiat : contrôle de plafond + réservation sont atomiques,
    même si plusieurs tâches cron écrivent en parallèle dans le journal."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def insert_call(conn: sqlite3.Connection, **fields) -> str:
    unknown = set(fields) - set(CALL_COLUMNS)
    if unknown:
        raise ValueError(f"colonnes inconnues : {sorted(unknown)}")
    fields.setdefault("call_id", uuid.uuid4().hex)
    ts = fields.get("ts") or utc_now()
    fields["ts"] = ts.astimezone(dt.timezone.utc).isoformat() if isinstance(ts, dt.datetime) else ts
    fields.setdefault("billing_month", fields["ts"][:7])
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO llm_calls ({cols}) VALUES ({marks})", tuple(fields.values()))
    return fields["call_id"]


def update_call(conn: sqlite3.Connection, call_id: str, **fields) -> None:
    unknown = set(fields) - set(CALL_COLUMNS)
    if unknown or "call_id" in fields:
        raise ValueError(f"colonnes non modifiables : {sorted(unknown | ({'call_id'} & set(fields)))}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE llm_calls SET {sets} WHERE call_id = ?", (*fields.values(), call_id))


def month_spend(conn: sqlite3.Connection, month: str, purpose: str, shop_id: str | None = None) -> float:
    """Cumul du mois de facturation (réel + réservations en cours), hors appels simulés.

    Repli sur le mois de `ts` quand billing_month est NULL (ligne écrite hors de insert_call), comme
    la vue v_llm_cost_monthly : une ligne sans mois de facturation ne doit jamais échapper au plafond."""
    sql = ("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE dry_run = 0 AND purpose = ? "
           "AND COALESCE(billing_month, substr(ts, 1, 7)) = ?")
    args: list = [purpose, month]
    if shop_id is not None:
        sql += " AND shop_id = ?"
        args.append(shop_id)
    return float(conn.execute(sql, args).fetchone()[0])


def expire_pending(conn: sqlite3.Connection, now: dt.datetime, ttl_minutes: float) -> list[sqlite3.Row]:
    """Passe en `abandoned` (cost_usd = 0) les réservations `pending` plus vieilles que `ttl_minutes`.
    À appeler dans la transaction de réservation. Renvoie les lignes libérées (avant mise à jour)."""
    cutoff = now - dt.timedelta(minutes=ttl_minutes)
    expired = [r for r in conn.execute("SELECT call_id, ts, agent, shop_id, purpose, cost_usd FROM llm_calls "
                                       "WHERE status = 'pending' ORDER BY ts")
               if dt.datetime.fromisoformat(r["ts"]) <= cutoff]
    for r in expired:
        conn.execute("UPDATE llm_calls SET status = 'abandoned', cost_usd = 0, error = ? "
                     "WHERE call_id = ? AND status = 'pending'",
                     (f"réservation expirée (> {ttl_minutes:g} min) : {r['cost_usd']:.4f} $ libérés", r["call_id"]))
    return expired


def record_alert_once(conn: sqlite3.Connection, key: str, ts: dt.datetime) -> bool:
    """Enregistre une clé d'alerte. Renvoie False si elle existait déjà (alerte déjà envoyée)."""
    cur = conn.execute("INSERT OR IGNORE INTO llm_alerts (alert_key, ts) VALUES (?, ?)", (key, ts.isoformat()))
    return cur.rowcount == 1
