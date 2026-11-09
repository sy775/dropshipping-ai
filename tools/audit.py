"""Étapes déterministes de /audit : pytest, scan de secrets, contrôle de mutations ; écrit docs/reports/NNN-titre.md.

    python -m tools.audit [--full] [--base origin/main]

- Branche attendue : spec/NNN-titre ; le rapport est docs/reports/NNN-titre.md.
- Première étape, « fichiers protégés inchangés » : échec si un fichier verrouillé présent sur la base est modifié
  ou supprimé sur la branche (commits depuis la base, ou arbre de travail). Liste unique tools/locked_paths.txt,
  partagée avec le hook ; on contrôle l'union de la liste de la branche et de celle de la base. Un fichier absent
  de la base (première introduction) n'est pas contrôlé ; base ou liste introuvable = échec. tools/mutations.yaml
  est en ajout seul : échec si une ligne présente à la base de la branche est modifiée ou supprimée.
  Une modification voulue se fait par Younes sur une branche human/*.
  `python -m tools.audit --protected-only` ne lance que ce contrôle (CI, toute branche sauf main et human/*).
- Chaque étape est bloquante : arrêt à la première en échec (les suivantes sont notées « non lancée »).
- Mutations : `--changed` par défaut ; toutes si `--full` ou si le catalogue diffère de la base (ou en est absent).
- Les sections rédigées après coup (Critic, Hypothèses, Écarts avec la documentation officielle) sont conservées
  quand le rapport est régénéré.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from journal.redact import redact

REPO_ROOT = Path(__file__).resolve().parents[1]
MANUAL_MARKER = "## Critic"
MANUAL_DEFAULT = """## Critic

_À compléter par /audit : tableau du sous-agent critic, puis tests rouges et corrections éventuels._

## Hypothèses

_À compléter._

## Écarts avec la documentation officielle

_À compléter (pages citées)._
"""


# Fichiers et dossiers verrouillés : liste unique partagée avec PROTECTED du hook, verrouillée elle-même.
LOCKED_LIST = "tools/locked_paths.txt"
CATALOGUE = "tools/mutations.yaml"                    # ajout seul (relecture externe)


def locked_files(text: str) -> set[str]:
    """Fichiers de la liste (les dossiers, finissant par « / », ne servent qu'à la règle par mention du hook)."""
    entries = (line.strip() for line in text.splitlines())
    return {e for e in entries if e and not e.startswith("#") and not e.endswith("/")}


GUARDED = tuple(sorted(locked_files((REPO_ROOT / LOCKED_LIST).read_text(encoding="utf-8"))))


@dataclass
class Step:
    name: str
    command: list[str]
    status: str = "non lancée"      # ok, échec, non lancée
    seconds: float = 0.0
    output: str = ""


Runner = Callable[[list[str], Path], tuple[int, str]]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")


def default_runner(command: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def report_name(branch: str) -> str:
    m = re.fullmatch(r"spec/(\d{3}-[a-z0-9-]+)", branch)
    if not m:
        raise SystemExit(f"branche {branch!r} : /audit se lance sur une branche spec/NNN-titre")
    return m.group(1)


def catalogue_changed(repo: Path, base: str) -> bool:
    if _git(repo, "cat-file", "-e", f"{base}:tools/mutations.yaml").returncode != 0:
        return True
    return _git(repo, "diff", "--quiet", base, "--", "tools/mutations.yaml").returncode != 0


def guarded_changes(repo: Path, base: str) -> list[str]:
    """Fichiers verrouillés modifiés ou supprimés depuis `base`, et catalogue réécrit ; base ou liste introuvable =
    échec (contrôle fermé). La liste de la base compte aussi : retirer une entrée sur la branche ne la soustrait pas."""
    if _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        return [f"(base {base} introuvable)"]
    local_list = repo / LOCKED_LIST
    base_list = _git(repo, "show", f"{base}:{LOCKED_LIST}")
    if not local_list.is_file() and base_list.returncode != 0:
        return [f"({LOCKED_LIST} introuvable)"]
    paths = locked_files(local_list.read_text(encoding="utf-8")) if local_list.is_file() else set()
    if base_list.returncode == 0:
        paths |= locked_files(base_list.stdout)
    changed = []
    for path in sorted(paths):
        if _git(repo, "cat-file", "-e", f"{base}:{path}").returncode != 0:
            continue                                                       # première introduction
        committed = _git(repo, "diff", "--quiet", f"{base}...HEAD", "--", path).returncode
        local = _git(repo, "diff", "--quiet", "HEAD", "--", path).returncode
        if committed != 0 or local != 0:
            changed.append(path)
    if not catalogue_append_only(repo, base):
        changed.append(f"{CATALOGUE} (ajout seul : une ligne de {base} a été modifiée ou supprimée)")
    return changed


def catalogue_append_only(repo: Path, base: str) -> bool:
    """Le catalogue de la merge-base est un préfixe du catalogue commité et de celui de l'arbre de travail."""
    merge_base = _git(repo, "merge-base", base, "HEAD").stdout.strip()
    if not merge_base:
        return False                                                       # contrôle fermé
    old = _git(repo, "show", f"{merge_base}:{CATALOGUE}")
    if old.returncode != 0:
        return True                                                        # première introduction
    head = _git(repo, "show", f"HEAD:{CATALOGUE}")
    work = repo / CATALOGUE
    texts = [head.stdout if head.returncode == 0 else "",
             work.read_bytes().decode("utf-8").replace("\r\n", "\n") if work.is_file() else ""]
    return all(text.startswith(old.stdout) for text in texts)


def guarded_step(repo: Path, base: str) -> Step:
    start = time.monotonic()
    changed = guarded_changes(repo, base)
    step = Step("fichiers protégés inchangés",
                [sys.executable, "-m", "tools.audit", "--protected-only", "--base", base])
    step.status, step.seconds = ("échec" if changed else "ok"), time.monotonic() - start
    step.output = (f"modifiés depuis {base} (à faire par Younes sur une branche human/*) :\n" + "\n".join(changed)
                   if changed else "aucun fichier protégé modifié")
    return step


def plan_steps(full: bool, base: str) -> list[Step]:
    py = sys.executable
    mutation = [py, "-m", "tools.mutation_check", "--base", base] + ([] if full else ["--changed"])
    return [
        Step("pytest complet", [py, "-m", "pytest", "-p", "no:cacheprovider"]),
        Step("secret_scan (arbre et historique)", [py, "-m", "tools.secret_scan"]),
        Step("mutation_check" + (" (complet)" if full else " (--changed)"), mutation),
    ]


def run_steps(steps: list[Step], cwd: Path, runner: Runner = default_runner) -> bool:
    for step in steps:
        start = time.monotonic()
        rc, output = runner(step.command, cwd)
        step.seconds, step.output = time.monotonic() - start, redact(output)
        step.status = "ok" if rc == 0 else "échec"
        if rc != 0:
            return False
    return True


def render(name: str, branch: str, head: str, commits: str, steps: list[Step], ok: bool, manual: str) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Rapport {name}", "",
             f"- Branche : `{branch}` ; HEAD : `{head}` ; audit du {now}.",
             f"- Résultat des étapes déterministes : **{'OK' if ok else 'ÉCHEC'}**.", "",
             "## Commits", "", "```", commits or "(aucun)", "```", "",
             "## Étapes de /audit", "", "| Étape | Résultat | Durée |", "| --- | --- | --- |"]
    lines += [f"| {s.name} | {s.status} | {s.seconds:.0f} s |" for s in steps]
    for s in steps:
        if s.status == "non lancée":
            continue
        shown = " ".join(["python", *s.command[1:]])
        lines += ["", f"### {s.name}", "", f"Commande : `{shown}`", "", "```", s.output or "(aucune sortie)", "```"]
    return "\n".join(lines) + "\n\n" + manual.strip() + "\n"


def main(argv: list[str] | None = None, runner: Runner = default_runner, repo: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.audit", description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="toutes les mutations")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--protected-only", action="store_true", help="seulement le contrôle des fichiers protégés")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.protected_only:
        step = guarded_step(repo, args.base)
        print(f"{step.name} : {step.status}\n{step.output}")
        return 0 if step.status == "ok" else 1
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    name = report_name(branch)
    full = args.full or catalogue_changed(repo, args.base)
    guard = guarded_step(repo, args.base)
    steps = plan_steps(full, args.base)
    ok = guard.status == "ok" and run_steps(steps, repo, runner)
    steps = [guard, *steps]

    path = repo / "docs" / "reports" / f"{name}.md"
    manual = MANUAL_DEFAULT
    if path.is_file():
        previous = path.read_text(encoding="utf-8")
        if MANUAL_MARKER in previous:
            manual = previous[previous.index(MANUAL_MARKER):]
    head = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    commits = _git(repo, "log", "--oneline", f"{args.base}..HEAD").stdout.strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(name, branch, head, commits, steps, ok, manual), encoding="utf-8")
    for s in steps:
        print(f"{s.name} : {s.status} ({s.seconds:.0f} s)")
    print(f"rapport : {path.relative_to(repo).as_posix()}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
