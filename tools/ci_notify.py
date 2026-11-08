"""Notification Telegram du statut de la CI (dernier job de .github/workflows/ci.yml).

    NEEDS_JSON='${{ toJSON(needs) }}' python -m tools.ci_notify

Le jeton et l'identifiant du chat sont lus dans l'environnement (secrets GitHub) par alerts.telegram, qui masque
le message et le jeton ; rien n'est affiché qui puisse les contenir. Sans secrets : rien n'est envoyé.
"""
from __future__ import annotations

import json
import os
import sys

from alerts.telegram import Notifier, TelegramNotifier


def build_message(needs: dict, env: dict) -> str:
    results = {job: (info or {}).get("result", "inconnu") for job, info in sorted(needs.items())}
    ok = all(r in ("success", "skipped") for r in results.values())
    detail = ", ".join(f"{job} {result}" for job, result in results.items())
    run_url = f"{env.get('GITHUB_SERVER_URL', '')}/{env.get('GITHUB_REPOSITORY', '')}/actions/runs/{env.get('GITHUB_RUN_ID', '')}"
    return (f"CI {'OK' if ok else 'ÉCHEC'} : {env.get('GITHUB_REPOSITORY', '?')} @ {env.get('GITHUB_REF_NAME', '?')} "
            f"({env.get('GITHUB_EVENT_NAME', '?')}) : {detail}\n{run_url}")


def main(notifier: Notifier | None = None, env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
    try:
        needs = json.loads(env.get("NEEDS_JSON") or "{}")
    except ValueError:
        needs = {}
    notifier = notifier or TelegramNotifier(env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID"))
    notifier.send(build_message(needs, env))
    print("notification envoyée (ou journalisée si Telegram n'est pas configuré)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
