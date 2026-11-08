"""Contrôle de mutations des garde-fous : chaque mutation de tools/mutations.yaml doit faire échouer un test.

    python -m tools.mutation_check [--only m001,m002] [--changed] [--base origin/main] [--jobs N]

Contrôles du catalogue (toujours sur toutes les entrées, échec sinon) :
- le fichier existe dans la branche ; champs id, description, file, old, new ; `id` uniques ; `new != old` ;
- `old` présent exactement une fois dans `file` (le catalogue suit le code) ;
- tous les `id` du catalogue de `<base>` (origin/main par défaut) existent encore. Exemption « première
  introduction » uniquement si le catalogue est absent de `<base>` ; référence `<base>` introuvable = échec.
Les entrées modifiées par rapport à `<base>` sont listées (avant / après).

Exécution : chaque mutation est appliquée dans une copie temporaire des fichiers du dépôt (suivis et non suivis
non ignorés : `.env` n'est jamais copié), puis `pytest -x -q` y est lancé. L'arbre de travail n'est jamais modifié.
Une mutation qui ne fait échouer aucun test est un échec. `--changed` limite l'exécution aux mutations des fichiers
modifiés depuis `<base>` et aux entrées ajoutées ou modifiées.
"""
from __future__ import annotations

import argparse
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = "tools/mutations.yaml"
FIELDS = ("id", "description", "file", "old", "new")
DEFAULT_TIMEOUT = 300


@dataclass
class Outcome:
    id: str
    status: str            # killed, survived, error
    seconds: float
    detail: str = ""


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)          # échecs du catalogue
    notes: list[str] = field(default_factory=list)
    changed_entries: list[tuple[dict, dict]] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    selected: int = 0
    total: int = 0
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors and all(o.status == "killed" for o in self.outcomes)


# ====================================================================== git

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=check)


def _exists(repo: Path, obj: str) -> bool:
    return _git(repo, "cat-file", "-e", obj, check=False).returncode == 0


# ====================================================================== catalogue

def _parse(text: str, where: str, report: Report) -> list[dict] | None:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        report.errors.append(f"{where} : YAML invalide ({exc})")
        return None
    if not isinstance(data, list):
        report.errors.append(f"{where} : une liste d'entrées est attendue")
        return None
    return data


def validate(entries: list[dict], repo: Path, report: Report) -> None:
    seen: set[str] = set()
    for i, e in enumerate(entries, 1):
        if not isinstance(e, dict) or any(k not in e for k in FIELDS):
            report.errors.append(f"entrée n°{i} : champs {', '.join(FIELDS)} obligatoires")
            continue
        mid = str(e["id"])
        if mid in seen:
            report.errors.append(f"{mid} : id en double")
        seen.add(mid)
        if e["new"] == e["old"]:
            report.errors.append(f"{mid} : new identique à old (mutation sans effet)")
        path = repo / e["file"]
        if not path.is_file():
            report.errors.append(f"{mid} : fichier introuvable {e['file']}")
            continue
        count = path.read_bytes().decode("utf-8").count(e["old"])
        if count != 1:
            report.errors.append(f"{mid} : `old` présent {count} fois dans {e['file']} (attendu : exactement 1)")


def compare_with_base(entries: list[dict], repo: Path, base: str, report: Report) -> None:
    if not _exists(repo, base):
        report.errors.append(f"référence de base introuvable : {base} (faire `git fetch`)")
        return
    if not _exists(repo, f"{base}:{CATALOGUE}"):
        report.notes.append(f"catalogue absent de {base} : première introduction ({len(entries)} entrées), "
                            "contrôle des id non applicable")
        report.added = [str(e.get("id")) for e in entries if isinstance(e, dict)]
        return
    text = _git(repo, "show", f"{base}:{CATALOGUE}").stdout.decode("utf-8")
    before = _parse(text, f"{base}:{CATALOGUE}", report)
    if before is None:
        return
    current = {str(e.get("id")): e for e in entries if isinstance(e, dict)}
    for old in before:
        oid = str(old.get("id"))
        if oid not in current:
            report.errors.append(f"{oid} : entrée de {base} supprimée ou id modifié (interdit)")
        elif any(old.get(k) != current[oid].get(k) for k in ("file", "old", "new", "description")):
            report.changed_entries.append((old, current[oid]))
    base_ids = {str(o.get("id")) for o in before}
    report.added = [i for i in current if i not in base_ids]


def changed_files(repo: Path, base: str) -> set[str]:
    merge_base = _git(repo, "merge-base", base, "HEAD").stdout.decode().strip()
    out = _git(repo, "diff", "--name-only", merge_base).stdout.decode("utf-8")      # commits + arbre de travail
    return {line for line in out.splitlines() if line}


# ====================================================================== exécution

def _copy_repo(repo: Path, dest: Path) -> None:
    names = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout.decode("utf-8")
    for name in filter(None, names.split("\0")):
        src = repo / name
        if src.is_file():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


def _pytest(workdir: Path, timeout: int) -> tuple[int | None, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("LLM_LIVE", "ANTHROPIC_API_KEY")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"], cwd=workdir,
                              env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "délai dépassé"
    lines = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()
    failed = [line for line in lines if line.startswith(("FAILED", "ERROR"))]
    return proc.returncode, (failed or lines[-1:] or [""])[0][:200]


def _run_one(entry: dict, workdir: Path, timeout: int) -> Outcome:
    start = time.monotonic()
    path = workdir / entry["file"]
    original = path.read_bytes()
    text = original.decode("utf-8")
    path.write_bytes(text.replace(entry["old"], entry["new"], 1).encode("utf-8"))
    try:
        rc, tail = _pytest(workdir, timeout)
    finally:
        path.write_bytes(original)
    secs = time.monotonic() - start
    if rc is None:
        return Outcome(entry["id"], "killed", secs, "tuée par délai dépassé (boucle ou blocage)")
    if rc in (1, 2, 3):                       # tests en échec, collecte interrompue, erreur interne
        return Outcome(entry["id"], "killed", secs, tail)
    if rc == 0:
        return Outcome(entry["id"], "survived", secs, "aucun test n'échoue")
    return Outcome(entry["id"], "error", secs, f"pytest code {rc} : {tail}")


def execute(entries: list[dict], repo: Path, jobs: int, timeout: int, report: Report) -> None:
    with tempfile.TemporaryDirectory(prefix="mutation-") as tmp:
        workdirs = [Path(tmp) / f"w{i}" for i in range(max(1, min(jobs, len(entries))))]
        for w in workdirs:
            _copy_repo(repo, w)
        rc, tail = _pytest(workdirs[0], timeout)
        if rc != 0:
            report.errors.append(f"suite de tests en échec sans mutation : {tail}")
            return
        pool: queue.Queue[Path] = queue.Queue()
        for w in workdirs:
            pool.put(w)

        def task(entry: dict) -> Outcome:
            w = pool.get()
            try:
                return _run_one(entry, w, timeout)
            finally:
                pool.put(w)

        with ThreadPoolExecutor(max_workers=len(workdirs)) as ex:
            report.outcomes = list(ex.map(task, entries))


# ====================================================================== orchestration

def run(repo: Path = REPO_ROOT, *, base: str = "origin/main", only: list[str] | None = None, changed: bool = False,
        jobs: int = 4, timeout: int = DEFAULT_TIMEOUT) -> Report:
    report = Report()
    start = time.monotonic()
    path = repo / CATALOGUE
    if not path.is_file():
        report.errors.append(f"{CATALOGUE} absent de la branche")
        return report
    entries = _parse(path.read_text(encoding="utf-8"), CATALOGUE, report)
    if entries is None:
        return report
    report.total = len(entries)
    validate(entries, repo, report)
    compare_with_base(entries, repo, base, report)
    if report.errors:
        report.seconds = time.monotonic() - start
        return report

    selected = entries
    if only:
        unknown = [i for i in only if i not in {e["id"] for e in entries}]
        if unknown:
            report.errors.append(f"--only : id inconnus {', '.join(unknown)}")
            return report
        selected = [e for e in entries if e["id"] in only]
    if changed:
        files = changed_files(repo, base)
        touched = {old["id"] for old, _ in report.changed_entries} | set(report.added)
        selected = [e for e in selected if e["file"] in files or e["id"] in touched]
        report.notes.append(f"--changed : {len(files)} fichier(s) modifié(s) depuis {base}")
    report.selected = len(selected)
    if selected:
        execute(selected, repo, jobs, timeout, report)
    report.seconds = time.monotonic() - start
    return report


def to_markdown(report: Report) -> str:
    killed = sum(o.status == "killed" for o in report.outcomes)
    lines = [f"**{'OK' if report.ok else 'ÉCHEC'}** : {killed} / {report.selected} mutation(s) tuée(s) "
             f"(catalogue : {report.total} entrées ; durée {report.seconds:.0f} s)."]
    lines += [f"- {n}" for n in report.notes]
    if report.errors:
        lines += ["", "Échecs du catalogue :"] + [f"- {e}" for e in report.errors]
    bad = [o for o in report.outcomes if o.status != "killed"]
    if bad:
        lines += ["", "Mutations non tuées :"] + [f"- {o.id} ({o.status}) : {o.detail}" for o in bad]
    if report.added and report.changed_entries is not None:
        lines += ["", f"Entrées ajoutées : {', '.join(report.added)}"]
    if report.changed_entries:
        lines += ["", "Entrées modifiées (avant / après) :"]
        for old, new in report.changed_entries:
            lines.append(f"- {old['id']}")
            for k in ("file", "old", "new", "description"):
                if old.get(k) != new.get(k):
                    lines.append(f"  - {k} : `{old.get(k)!r}` → `{new.get(k)!r}`")
    timed = [o for o in report.outcomes if "délai" in o.detail]
    if timed:
        lines += ["", "Tuées par délai : " + ", ".join(o.id for o in timed)]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.mutation_check", description=__doc__.splitlines()[0])
    parser.add_argument("--only", default=None, help="ids séparés par des virgules (m001,m002)")
    parser.add_argument("--changed", action="store_true", help="mutations des fichiers modifiés depuis --base")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="secondes par mutation")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    only = [i.strip() for i in args.only.split(",") if i.strip()] if args.only else None
    report = run(args.repo, base=args.base, only=only, changed=args.changed, jobs=args.jobs, timeout=args.timeout)
    print(to_markdown(report), end="")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
