"""Hook PreToolUse : bloque (code de sortie 2, raison sur stderr) les actions interdites par la spec 003.

Doc : https://code.claude.com/docs/en/hooks (« Exit code 2 » : bloque l'appel, stderr est transmis à Claude).

- Écriture ou édition d'un fichier verrouillé (liste unique tools/locked_paths.txt, lue aussi par le filet de
  tools/audit.py) et de tout fichier .env* (sauf .env.example). Liste illisible : tout est bloqué.
- Commande Bash ou PowerShell qui mentionne un fichier ou un dossier verrouillé (relecture externe, critic) : permise
  seulement dans `git diff|log|show|status` sans redirection ; .env* toujours bloqué, même en lecture.
- Read, Grep, Glob visant .env* : les règles deny `Read` ne couvrent Grep et Glob qu'en « best-effort »
  (https://code.claude.com/docs/en/permissions, « What Read and Edit rules cover »).
- Commandes (Bash, PowerShell) : `--force`, `gh repo edit`, `gh secret`, `git filter-repo`, `gh pr merge`. Seul
  `git push [-u] origin spec/<nom>` est permis (ni main, ni human/*, ni refspec avec deux-points, ni --delete).
Lancé par `python -I` (critic, bloquant 1) : ni le dossier du script ni PYTHONPATH en tête de sys.path, donc un
module déposé à côté du hook n'est pas importé.
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
import sys


def _err(text: str) -> None:
    """stderr en UTF-8, quelle que soit la console (Windows : cp1252 par défaut)."""
    sys.stderr.buffer.write((text + "\n").encode("utf-8"))
    sys.stderr.flush()


# Fichiers et dossiers verrouillés : une seule liste, tools/locked_paths.txt (verrouillée elle-même), lue aussi par
# le filet de tools/audit.py et de la CI. Une modification voulue se fait par Younes (branche human/* après fusion).
LOCKED_LIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "locked_paths.txt")


def load_locked(path: str = LOCKED_LIST) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(fichiers, dossiers) de la liste ; un dossier finit par « / », « # » commence un commentaire."""
    with open(path, encoding="utf-8") as f:
        entries = [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
    return tuple(e for e in entries if not e.endswith("/")), tuple(e for e in entries if e.endswith("/"))


try:
    PROTECTED, PROTECTED_DIRS = load_locked()
except (OSError, UnicodeDecodeError):
    PROTECTED = PROTECTED_DIRS = None                    # liste illisible : decide() bloque tout
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
READ_TOOLS = {"Read", "Grep", "Glob"}
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


# ====================================================================== mentions dans une commande
# Relecture externe de la spec 003 : un fichier protégé se modifie aussi par Bash ou PowerShell (>>, cp, sed -i,
# Add-Content...). Toute commande dont le texte le mentionne est donc bloquée, quelle que soit l'action.
# Mention « en position fermée » : précédée d'un délimiteur (début, espace, guillemet, séparateur de chemin...) et
# non suivie d'un caractère de mot. Limite connue : globs, variables et chemins assemblés ne sont pas vus.

_LEFT = r"(?:^|(?<=[\s'\"`/\\=<>(:;,|&\[{+]))"
_ENV_MENTION = re.compile(_LEFT + r"(\.env[\w.\-]*)", re.IGNORECASE)
_GIT_READ_ONLY = {"diff", "log", "show", "status"}
_UNSAFE_IN_GIT_READ = re.compile(r"[<>`]|\$\(|--output|--ext-diff|--textconv")


def _basename_mention(name: str) -> re.Pattern:
    return re.compile(_LEFT + re.escape(name) + r"(?![\w])", re.IGNORECASE)


def _dir_mention(path: str) -> re.Pattern:
    """Dossier nommé seul (`tools/hooks`, `tools/hooks/`, `tools/hooks/.`, `tools/hooks/*`) : critic, bloquant 4."""
    return re.compile(_LEFT + re.escape(path.rstrip("/")) + r"/?(?:\.|\*)?(?=$|[\s'\"`;|&)])", re.IGNORECASE)


_PROTECTED_MENTIONS = tuple((p, _basename_mention(p.rsplit("/", 1)[-1])) for p in PROTECTED or ())
_PROTECTED_MENTIONS += tuple((d, _dir_mention(d)) for d in PROTECTED_DIRS or ())


def env_mention(text: str) -> str | None:
    """Premier nom .env* (sauf .env.example) mentionné dans `text`."""
    for m in _ENV_MENTION.finditer(text):
        if m.group(1).lower() != ".env.example":
            return m.group(1)
    return None


def _is_git_read(segment: str) -> bool:
    """`git diff|log|show|status ...` sans option globale (sauf --no-pager), redirection ni substitution."""
    if _UNSAFE_IN_GIT_READ.search(segment):
        return False
    tokens = _tokens(segment.strip())
    if not tokens or tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower() not in ("git", "git.exe"):
        return False
    rest = tokens[1:]
    while rest and rest[0] == "--no-pager":
        rest = rest[1:]
    return bool(rest) and rest[0] in _GIT_READ_ONLY


def check_mentions(command: str) -> str | None:
    text = command.replace("\\", "/")
    env = env_mention(text)
    if env:
        return f"la commande mentionne {env} : les fichiers .env* (sauf .env.example) sont interdits, même en lecture"
    for segment in _SEGMENTS.split(text):
        for protected, pattern in _PROTECTED_MENTIONS:
            if pattern.search(segment) and not _is_git_read(segment):
                return (f"la commande mentionne {protected} (protégé) : seules les lectures git diff, log, show "
                        "et status sont permises")
    return None


# Champs de chemin par outil (doc hooks, « PreToolUse input ») ; le `pattern` de Grep porte sur le contenu.
_READ_FIELDS = {"Read": ("file_path",), "Grep": ("path", "glob"), "Glob": ("pattern", "path")}


def check_read(tool: str, tool_input: dict) -> str | None:
    """Read, Grep, Glob : chemin ou motif de fichiers visant .env*."""
    for key in _READ_FIELDS[tool]:
        value = tool_input.get(key)
        if isinstance(value, str) and env_mention(value.replace("\\", "/")):
            return f"{value} : les fichiers .env* (sauf .env.example) ne sont jamais lus par Claude"
    return None


# ====================================================================== commandes

def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


_UNKNOWN = "<indéterminée>"
_CREATE_OPTIONS = ("-c", "-C", "-b", "-B", "--create", "--orphan", "--" + "force-create")


def _switch_target(args: list[str]) -> str | None:
    """Branche visée par `git switch|checkout <args>` ; None si ce n'est pas un changement de branche."""
    if "--" in args:
        return None                                      # checkout -- fichier : restauration de fichiers
    it = iter(args)
    for a in it:
        if a in _CREATE_OPTIONS:
            return next(it, _UNKNOWN)
        if a == "-":
            return _UNKNOWN                              # branche précédente : inconnue ici
        if not a.startswith("-"):
            return a
    return None


_SPEC_BRANCH = re.compile(r"spec/[A-Za-z0-9._-]+")
_PUSH_OPTIONS = {"-u", "--set-upstream"}


def _check_push(args: list[str], cwd: str, branch: str | None = None) -> str | None:
    """Seule forme permise : `git push [-u] origin spec/<nom>` (critic, bloquant 3, et relecture externe).

    Interdits : main et human/* (branche exemptée du filet CI), refspec avec deux-points (HEAD:x, :x), --delete,
    -f, --force*, +refspec, --all, --mirror, --tags, push sans branche écrite. `cwd` et `branch` ne servent plus :
    la branche visée est toujours écrite dans la commande.
    """
    options = [a for a in args if a.startswith("-")]
    positional = [a for a in args if not a.startswith("-")]
    for option in options:
        if option not in _PUSH_OPTIONS:
            return f"git push {option} : option interdite (seul -u est permis)"
    if len(positional) != 2 or positional[0] != "origin" or not _SPEC_BRANCH.fullmatch(positional[1]):
        return ("push interdit : seule la forme `git push origin spec/<nom>` est permise "
                "(ni main, ni human/*, ni refspec avec deux-points)")
    return None


def _check_git(tokens: list[str], cwd: str, state: dict) -> str | None:
    """`state["branch"]` : branche courante simulée, mise à jour par switch/checkout dans la commande."""
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
    if tokens[i] in ("switch", "checkout"):
        target = _switch_target(tokens[i + 1:])
        if target is not None:
            state["branch"] = target
        return None
    if tokens[i] == "push":
        return _check_push(tokens[i + 1:], cwd, state.get("branch"))
    return None


def check_command(command: str, cwd: str) -> str | None:
    for pattern, label in FORBIDDEN:
        if pattern.search(command):
            return f"commande interdite : {label}"
    state: dict = {}
    for segment in _SEGMENTS.split(command):
        tokens = _tokens(segment.strip())
        for j, tok in enumerate(tokens):
            base = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if base in ("git", "git.exe"):
                reason = _check_git(tokens[j:], cwd, state)
                if reason:
                    return reason
                break
    return None


# ====================================================================== point d'entrée

def decide(data: dict) -> str | None:
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd", "")
    if PROTECTED is None:
        return f"liste des fichiers verrouillés illisible ({LOCKED_LIST}) : action bloquée par prudence"
    if tool in WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return check_path(path, project_dir)
    if tool in SHELL_TOOLS:
        command = tool_input.get("command", "")
        return check_mentions(command) or check_command(command, data.get("cwd") or project_dir)
    if tool in READ_TOOLS:
        return check_read(tool, tool_input)
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
