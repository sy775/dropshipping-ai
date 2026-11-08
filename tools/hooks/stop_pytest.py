"""Hook Stop : en fin de tour, lance `pytest -x -q` si des fichiers .py ont changé depuis le dernier passage vert.

Doc : https://code.claude.com/docs/en/hooks (« Exit code 2 behavior per event » : pour Stop, « Prevents Claude
from stopping, continues the conversation » ; stderr devient la consigne) et
https://code.claude.com/docs/en/hooks-guide (« Stop hook hits the block cap » : sortir en 0 si stop_hook_active).

- `stop_hook_active` vrai : sortie 0 (Claude a déjà été relancé une fois ; /audit et la CI restent les garde-fous).
- Empreinte SHA-256 des .py (suivis et non suivis non ignorés) ; si elle égale celle du dernier passage vert
  (.pytest_cache/claude-stop-green), rien n'est lancé : une simple discussion ne coûte rien.
- Vert : l'empreinte est enregistrée, sortie 0. Rouge : fin du rapport sur stderr, sortie 2.
Bibliothèque standard uniquement.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def _err(text: str) -> None:
    """stderr en UTF-8, quelle que soit la console (Windows : cp1252 par défaut)."""
    sys.stderr.buffer.write((text + "\n").encode("utf-8"))
    sys.stderr.flush()


STAMP = Path(".pytest_cache") / "claude-stop-green"
TIMEOUT = 540


def _python(project: Path) -> str:
    override = os.environ.get("CLAUDE_STOP_PYTEST_PYTHON")
    if override:
        return override
    for candidate in (project / ".venv" / "Scripts" / "python.exe", project / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def digest(project: Path) -> str:
    out = subprocess.run(["git", "-C", str(project), "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                          "--", "*.py"], capture_output=True, check=True).stdout.decode("utf-8")
    h = hashlib.sha256()
    for name in sorted(filter(None, out.split("\0"))):
        path = project / name
        h.update(name.encode("utf-8") + b"\0")
        h.update(path.read_bytes() if path.is_file() else b"<deleted>")
        h.update(b"\0")
    return h.hexdigest()


def main() -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        data = {}
    if data.get("stop_hook_active"):
        return 0
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or ".")
    try:
        current = digest(project)
    except (OSError, subprocess.SubprocessError) as exc:
        _err(f"hook stop_pytest : empreinte impossible ({exc}), tests non lancés")
        return 0
    stamp = project / STAMP
    if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == current:
        return 0
    try:
        proc = subprocess.run([_python(project), "-m", "pytest", "-x", "-q"], cwd=project, capture_output=True,
                              timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.buffer.write(f"pytest -x -q n'a pas abouti en fin de tour : {exc}\n".encode("utf-8"))
        return 2
    if proc.returncode == 0:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(current + "\n", encoding="utf-8")
        return 0
    tail = "\n".join(proc.stdout.decode("utf-8", errors="replace").splitlines()[-40:])
    sys.stderr.buffer.write(("pytest -x -q échoue en fin de tour : corrige avant de terminer (ou explique pourquoi "
                             "le test est rouge volontairement).\n" + tail + "\n").encode("utf-8"))
    return 2


if __name__ == "__main__":
    sys.exit(main())
