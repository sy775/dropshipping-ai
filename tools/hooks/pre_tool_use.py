"""Hook PreToolUse : bloque (code de sortie 2, raison sur stderr) les actions interdites par la spec 003.

Doc : https://code.claude.com/docs/en/hooks (« Exit code 2 » : bloque l'appel, stderr est transmis à Claude).

- Écriture ou édition de tests/test_audit_guards.py et de tout fichier .env* (sauf .env.example).
- Commandes (Bash, PowerShell) : push vers main, `--force`, `gh repo edit`, `gh secret`, `git filter-repo`,
  `gh pr merge`. Sont aussi traités comme push forcé : `git push -f`, une refspec `+...`, `--mirror`, `--all`.
Ce contrôle par analyse de texte n'est pas une frontière de sécurité absolue : les règles `permissions.deny` de
.claude/settings.json en sont la seconde barrière.
Bibliothèque standard uniquement.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import subprocess
import sys


def _err(text: str) -> None:
    """stderr en UTF-8, quelle que soit la console (Windows : cp1252 par défaut)."""
    sys.stderr.buffer.write((text + "\n").encode("utf-8"))
    sys.stderr.flush()


PROTECTED = ("tests/test_audit_guards.py",)
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
FORBIDDEN = (
    (re.compile(r"--force"), "option --force"),
    (re.compile(r"\bgh\s+repo\s+edit\b"), "gh repo edit"),
    (re.compile(r"\bgh\s+secret\b"), "gh secret"),
    (re.compile(r"\bgit\b.*\bfilter-repo\b"), "git filter-repo"),
    (re.compile(r"\bgh\s+pr\s+merge\b"), "gh pr merge"),
)
_SEGMENTS = re.compile(r"\|\||&&|[;|&\n]")
# Options globales de git qui prennent une valeur dans l'argument suivant.
_GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


# ====================================================================== fichiers

def _relative(path: str, project_dir: str) -> str:
    p = posixpath.normpath(path.replace("\\", "/"))
    root = posixpath.normpath(project_dir.replace("\\", "/")) if project_dir else ""
    if root and p.lower().startswith(root.lower() + "/"):
        p = p[len(root) + 1:]
    return p[2:] if p.startswith("./") else p


def check_path(path: str, project_dir: str) -> str | None:
    rel = _relative(path, project_dir)
    low = rel.lower()
    for protected in PROTECTED:
        if low == protected or low.endswith("/" + protected):
            return f"{protected} est protégé (garde-fous d'audit) : modification interdite"
    name = low.rsplit("/", 1)[-1]
    if name.startswith(".env") and name != ".env.example":
        return f"{rel} : les fichiers .env* (sauf .env.example) ne sont jamais écrits par Claude"
    return None


# ====================================================================== commandes

def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _current_branch(cwd: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", cwd or ".", "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _is_main(ref: str) -> bool:
    ref = ref.lstrip("+")
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/"):]
    return ref == "main"


def _check_push(args: list[str], cwd: str) -> str | None:
    """`args` : arguments après `push`."""
    positional = []
    for a in args:
        if a in ("-f", "--mirror", "--all") or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]
                                               and a not in ("-u",)):
            return f"git push {a} : push forcé ou global interdit"
        if not a.startswith("-"):
            positional.append(a)
    refspecs = positional[1:]
    for spec in refspecs:
        if spec.startswith("+"):
            return f"git push {spec} : refspec forcée interdite"
        src, _, dst = spec.partition(":")
        if _is_main(dst or src):
            return "push vers main interdit : pousser la branche spec/* (la fusion reste humaine)"
        if not dst and src == "HEAD" and _current_branch(cwd) == "main":
            return "push de HEAD depuis main interdit"
    if not refspecs:
        branch = _current_branch(cwd)
        if branch is None:
            return "git push sans refspec et branche courante indéterminée : préciser la branche spec/*"
        if branch == "main":
            return "push vers main interdit (branche courante main) : pousser une branche spec/*"
    return None


def _check_git(tokens: list[str], cwd: str) -> str | None:
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        opt = tokens[i]
        if opt in _GIT_GLOBAL_WITH_VALUE and i + 1 < len(tokens):
            if opt == "-C":
                cwd = os.path.join(cwd, tokens[i + 1]) if cwd else tokens[i + 1]
            i += 2
        else:
            i += 1
    if i >= len(tokens):
        return None
    if tokens[i] == "filter-repo":
        return "git filter-repo interdit"
    if tokens[i] == "push":
        return _check_push(tokens[i + 1:], cwd)
    return None


def check_command(command: str, cwd: str) -> str | None:
    for pattern, label in FORBIDDEN:
        if pattern.search(command):
            return f"commande interdite : {label}"
    for segment in _SEGMENTS.split(command):
        tokens = _tokens(segment.strip())
        for j, tok in enumerate(tokens):
            base = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if base in ("git", "git.exe"):
                reason = _check_git(tokens[j:], cwd)
                if reason:
                    return reason
                break
    return None


# ====================================================================== point d'entrée

def decide(data: dict) -> str | None:
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd", "")
    if tool in WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return check_path(path, project_dir)
    if tool in SHELL_TOOLS:
        return check_command(tool_input.get("command", ""), data.get("cwd") or project_dir)
    return None


def main() -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        _err("hook pre_tool_use : entrée JSON illisible, action bloquée par prudence")
        return 2
    reason = decide(data)
    if reason:
        sys.stderr.buffer.write(f"Bloqué par tools/hooks/pre_tool_use.py : {reason}\n".encode("utf-8"))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
