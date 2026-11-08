"""Étapes déterministes de /audit : pytest, scan de secrets, contrôle de mutations ; écrit docs/reports/NNN-titre.md.

    python -m tools.audit [--full] [--base origin/main]

- Branche attendue : spec/NNN-titre ; le rapport est docs/reports/NNN-titre.md.
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
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    name = report_name(branch)
    full = args.full or catalogue_changed(repo, args.base)
    steps = plan_steps(full, args.base)
    ok = run_steps(steps, repo, runner)

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
