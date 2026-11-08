"""Outil manuel pour les réservations de lot perdues (identifiant provisoire `pending-<call_id>`).

Cas d'usage : le processus est mort entre l'envoi d'un lot et l'écriture de l'identifiant du fournisseur.
La réservation reste comptée dans le plafond, et check_stale_batches alerte après batch.stale_after_hours.
Après vérification dans la console du fournisseur :

    python -m journal.release_reservation list
    python -m journal.release_reservation attach <call_id> <batch_id>        # lot retrouvé : collecte normale
    python -m journal.release_reservation release <call_id> --reason "..." --confirm   # lot jamais envoyé

- `attach` écrit l'identifiant réel ; le cron `collect_batch` remplacera ensuite la réservation par le coût réel.
- `release` passe la ligne en `abandoned`, cost_usd = 0. À n'utiliser que si le lot n'existe pas chez le
  fournisseur (sinon il serait facturé hors plafond).
Seules les lignes `reserved`, à identifiant provisoire et plus vieilles que batch.stale_after_hours sont
concernées. Chaque opération est journalisée (date, motif masqué) dans le champ `error` de la ligne et dans
les logs, sans donnée client.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sqlite3
import sys
from dataclasses import dataclass

from journal import db
from journal.redact import redact

log = logging.getLogger(__name__)

PROVISIONAL_PREFIX = "pending-"          # même préfixe que llm.adapter.PROVISIONAL_BATCH_PREFIX
MAX_REASON_LEN = 200


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class LostReservation:
    call_id: str
    agent: str
    model: str
    shop_id: str | None
    ts: dt.datetime
    age_hours: float
    reserved_usd: float
    batch_id: str


def _is_provisional(row: sqlite3.Row) -> bool:
    return row["batch_id"] is None or str(row["batch_id"]).startswith(PROVISIONAL_PREFIX)


def _as_lost(row: sqlite3.Row, now: dt.datetime) -> LostReservation:
    ts = dt.datetime.fromisoformat(row["ts"])
    return LostReservation(row["call_id"], row["agent"], row["model"], row["shop_id"], ts,
                           (now - ts).total_seconds() / 3600, float(row["cost_usd"] or 0),
                           row["batch_id"] or f"{PROVISIONAL_PREFIX}{row['call_id']}")


def list_lost(conn: sqlite3.Connection, now: dt.datetime, min_age_hours: float) -> list[LostReservation]:
    rows = conn.execute("SELECT * FROM llm_calls WHERE status = 'reserved' ORDER BY ts").fetchall()
    lost = [_as_lost(r, now) for r in rows if _is_provisional(r)]
    return [r for r in lost if r.age_hours >= min_age_hours]


def _eligible(conn: sqlite3.Connection, call_id: str, now: dt.datetime, min_age_hours: float) -> LostReservation:
    row = conn.execute("SELECT * FROM llm_calls WHERE call_id = ?", (call_id,)).fetchone()
    if row is None:
        raise ReleaseError(f"call_id inconnu : {call_id}")
    if row["status"] != "reserved":
        raise ReleaseError(f"refus : la ligne {call_id} est au statut {row['status']!r}, pas 'reserved'")
    if not _is_provisional(row):
        raise ReleaseError(f"refus : la ligne {call_id} a déjà un identifiant fournisseur ({row['batch_id']}) ; "
                           f"le cron collect_batch s'en charge")
    lost = _as_lost(row, now)
    if lost.age_hours < min_age_hours:
        raise ReleaseError(f"refus : réservation de {lost.age_hours:.1f} h, âge minimal {min_age_hours:g} h "
                           f"(batch.stale_after_hours) ; l'envoi est peut-être encore en cours")
    return lost


def _provisional_clause() -> str:
    return f"status = 'reserved' AND (batch_id IS NULL OR batch_id LIKE '{PROVISIONAL_PREFIX}%')"


def attach(conn: sqlite3.Connection, call_id: str, batch_id: str, now: dt.datetime,
           min_age_hours: float) -> LostReservation:
    batch_id = batch_id.strip()
    if not batch_id or batch_id.startswith(PROVISIONAL_PREFIX):
        raise ReleaseError("refus : identifiant de lot fournisseur attendu (pas un identifiant provisoire)")
    with db.transaction(conn):
        lost = _eligible(conn, call_id, now, min_age_hours)
        if conn.execute("SELECT 1 FROM llm_calls WHERE batch_id = ?", (batch_id,)).fetchone():
            raise ReleaseError(f"refus : le lot {batch_id} est déjà rattaché à une autre ligne")
        note = f"identifiant rattaché manuellement le {now.isoformat(timespec='seconds')} (était {lost.batch_id})"
        cur = conn.execute(f"UPDATE llm_calls SET batch_id = ?, error = ? WHERE call_id = ? AND {_provisional_clause()}",
                           (batch_id, note, call_id))
        if cur.rowcount != 1:
            raise ReleaseError(f"refus : la ligne {call_id} a changé pendant l'opération")
    log.warning("réservation %s : lot %s rattaché manuellement (%.4f $ restent réservés jusqu'à la collecte)",
                call_id, batch_id, lost.reserved_usd)
    return lost


def release(conn: sqlite3.Connection, call_id: str, reason: str, confirm: bool, now: dt.datetime,
            min_age_hours: float) -> LostReservation:
    if not confirm:
        raise ReleaseError("refus : --confirm est obligatoire (la réservation sera libérée, cost_usd = 0)")
    reason = redact(" ".join((reason or "").split()))[:MAX_REASON_LEN]
    if not reason:
        raise ReleaseError("refus : --reason est obligatoire")
    with db.transaction(conn):
        lost = _eligible(conn, call_id, now, min_age_hours)
        note = (f"libération manuelle le {now.isoformat(timespec='seconds')} : {reason} "
                f"({lost.reserved_usd:.4f} $ libérés)")
        cur = conn.execute(f"UPDATE llm_calls SET status = 'abandoned', cost_usd = 0, error = ? "
                           f"WHERE call_id = ? AND {_provisional_clause()}", (note, call_id))
        if cur.rowcount != 1:
            raise ReleaseError(f"refus : la ligne {call_id} a changé pendant l'opération")
    log.warning("réservation %s libérée manuellement : %.4f $ (motif : %s)", call_id, lost.reserved_usd, reason)
    return lost


# ====================================================================== CLI

def main(argv: list[str] | None = None, conn: sqlite3.Connection | None = None,
         min_age_hours: float | None = None, now: dt.datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m journal.release_reservation",
                                     description="Réservations de lot à identifiant provisoire.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="réservations provisoires plus vieilles que batch.stale_after_hours")
    p_att = sub.add_parser("attach", help="rattacher l'identifiant de lot retrouvé chez le fournisseur")
    p_att.add_argument("call_id")
    p_att.add_argument("batch_id")
    p_rel = sub.add_parser("release", help="libérer une réservation (lot jamais envoyé)")
    p_rel.add_argument("call_id")
    p_rel.add_argument("--reason", required=True)
    p_rel.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)

    if conn is None or min_age_hours is None:
        from llm.config import load_settings          # import tardif : l'outil reste utilisable seul
        from llm.runtime import load_env
        load_env()
        conn = conn or db.connect()
        min_age_hours = min_age_hours if min_age_hours is not None else load_settings().batch_stale_hours
    now = now or db.utc_now()

    try:
        if args.cmd == "list":
            lost = list_lost(conn, now, min_age_hours)
            if not lost:
                print(f"Aucune réservation provisoire de plus de {min_age_hours:g} h.")
            for r in lost:
                print(f"{r.call_id}  agent={r.agent}  modèle={r.model}  âge={r.age_hours:.1f} h  "
                      f"réservé={r.reserved_usd:.4f} $  envoyé le {r.ts.isoformat(timespec='minutes')}")
        elif args.cmd == "attach":
            lost = attach(conn, args.call_id, args.batch_id, now, min_age_hours)
            print(f"Lot {args.batch_id} rattaché à {lost.call_id} : collect_batch le collectera.")
        else:
            lost = release(conn, args.call_id, args.reason, args.confirm, now, min_age_hours)
            print(f"Réservation {lost.call_id} libérée : {lost.reserved_usd:.4f} $ (statut abandoned).")
    except ReleaseError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    from journal.redact import install_redaction
    logging.basicConfig(level=logging.INFO)
    install_redaction()
    sys.exit(main())
