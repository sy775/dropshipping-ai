"""Hook PreToolUse du sous-agent critic : Bash limité à `git diff`, `git log` et pytest.

Doc : https://code.claude.com/docs/en/sub-agents (le champ `tools` n'accepte pas de filtre par commande) et
https://code.claude.com/docs/en/hooks (« Hooks in skills and agents » : hooks déclarés dans le frontmatter,
actifs seulement pendant l'exécution du sous-agent ; code 2 = appel bloqué).

Refusé : tout enchaînement ou redirection (; & | < > ` $ retour à la ligne), `--output` (écrit un fichier),
`--ext-diff` (lance un programme externe), et toute autre commande.
Bibliothèque standard uniquement.
"""
from __future__ import annotations

import json
import re
import shlex
import sys


def _err(text: str) -> None:
    """stderr en UTF-8, quelle que soit la console (Windows : cp1252 par défaut)."""
    sys.stderr.buffer.write((text + "\n").encode("utf-8"))
    sys.stderr.flush()


_META = re.compile(r"[;&|<>`$\n\r]")
_PYTHONS = {"python", "python3", "python.exe", "py"}
_DENIED_OPTIONS = ("--output", "--ext-diff")


def check(command: str) -> str | None:
    if _META.search(command):
        return "enchaînement, redirection ou substitution interdits"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "commande illisible"
    if not tokens:
        return "commande vide"
    if any(t.startswith(_DENIED_OPTIONS) for t in tokens):
        return "options --output et --ext-diff interdites"
    exe = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if exe in ("git", "git.exe"):
        if len(tokens) > 1 and tokens[1] in ("diff", "log"):
            return None
        return "seuls git diff et git log sont autorisés"
    if exe == "pytest":
        return None
    if exe in _PYTHONS and tokens[1:3] == ["-m", "pytest"]:
        return None
    return "commande non autorisée pour le critic (git diff, git log, pytest uniquement)"


def main() -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        _err("hook critic_bash : entrée JSON illisible")
        return 2
    command = (data.get("tool_input") or {}).get("command", "")
    reason = check(command)
    if reason:
        sys.stderr.buffer.write(f"Bloqué (critic en lecture seule) : {reason}\n".encode("utf-8"))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
