"""Scan de secrets : arbre de travail et historique git complet, avec les motifs de journal/redact.py.

    python -m tools.secret_scan [--repo CHEMIN] [--no-history]

- Motifs : ceux de `journal.redact._PATTERNS` (clés et jetons ; les emails ne sont pas des secrets).
- Arbre : fichiers suivis et fichiers non suivis non ignorés (`.env` est ignoré par git, donc jamais lu).
- Historique : chaque blob de chaque commit de `git rev-list --all`, avec son chemin.
- Exceptions : liste explicite `EXCEPTIONS` ci-dessous, chacune limitée à un fichier et à une portée.
- Aucune valeur trouvée n'est affichée : la ligne est masquée par `redact` avant impression.
- Code de sortie 0 si rien n'est trouvé, 1 sinon.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from journal.redact import _PATTERNS, redact

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Exception_:
    file: str            # chemin exact, relatif à la racine du dépôt
    scopes: frozenset    # {"tree"}, {"history"} ou les deux
    line: re.Pattern     # la ligne entière doit correspondre
    why: str


# La chaîne du faux jeton est assemblée à l'exécution, pour que ce fichier ne contienne aucun littéral détectable.
_FAKE_BEARER_LINE = re.escape("Bear" + "er abc.def-123")

EXCEPTIONS: tuple[Exception_, ...] = (
    Exception_("journal/redact.py", frozenset({"tree", "history"}), re.compile(r'\s*re\.compile\(r".*'),
               "définition des expressions régulières de masquage"),
    Exception_("tests/test_alerts.py", frozenset({"history"}), re.compile(r".*" + _FAKE_BEARER_LINE + r".*"),
               "faux en-tête Authorization écrit en clair avant la spec 003 (historique uniquement)"),
)


@dataclass(frozen=True)
class Finding:
    scope: str           # tree ou history
    path: str
    line_no: int
    pattern_no: int      # index du motif dans redact._PATTERNS, à partir de 1
    where: str           # « arbre » ou identifiant de blob
    excerpt: str         # ligne masquée

    def __str__(self) -> str:
        return (f"{self.scope} {self.where} {self.path}:{self.line_no} motif n°{self.pattern_no} : {self.excerpt}")


def _git(repo: Path, *args: str, input_: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], input=input_, capture_output=True, check=True).stdout


def _excepted(scope: str, path: str, line: str) -> bool:
    return any(e.file == path and scope in e.scopes and e.line.fullmatch(line) for e in EXCEPTIONS)


def scan_text(scope: str, path: str, text: str, where: str) -> list[Finding]:
    found = []
    for no, line in enumerate(text.splitlines(), 1):
        for i, pattern in enumerate(_PATTERNS, 1):
            if pattern.search(line) and not _excepted(scope, path, line):
                found.append(Finding(scope, path, no, i, where, redact(line.strip())[:160]))
                break
    return found


def _decode(data: bytes) -> str | None:
    if b"\0" in data[:8000]:
        return None                                          # binaire
    return data.decode("utf-8", errors="replace")


def scan_tree(repo: Path) -> list[Finding]:
    names = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode("utf-8").split("\0")
    found = []
    for name in sorted({n for n in names if n}):
        path = repo / name
        if not path.is_file():
            continue                                         # supprimé dans l'arbre mais encore indexé
        text = _decode(path.read_bytes())
        if text is not None:
            found += scan_text("tree", name, text, "arbre")
    return found


def _history_blobs(repo: Path) -> dict[str, set[str]]:
    """{blob: {chemins}} sur tous les commits de toutes les références."""
    blobs: dict[str, set[str]] = {}
    for commit in _git(repo, "rev-list", "--all").decode().split():
        for entry in _git(repo, "ls-tree", "-r", "-z", commit).decode("utf-8").split("\0"):
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            _mode, kind, sha = meta.split()
            if kind == "blob":
                blobs.setdefault(sha, set()).add(path)
    return blobs


def _read_blobs(repo: Path, shas: list[str]) -> dict[str, bytes]:
    out = _git(repo, "cat-file", "--batch", input_="".join(f"{s}\n" for s in shas).encode())
    contents, pos = {}, 0
    for sha in shas:
        header_end = out.index(b"\n", pos)
        _sha, _kind, size = out[pos:header_end].split()
        start = header_end + 1
        contents[sha] = out[start:start + int(size)]
        pos = start + int(size) + 1
    return contents


def scan_history(repo: Path) -> list[Finding]:
    blobs = _history_blobs(repo)
    contents = _read_blobs(repo, sorted(blobs))
    found = []
    for sha, paths in sorted(blobs.items()):
        text = _decode(contents[sha])
        if text is None:
            continue
        for path in sorted(paths):
            found += scan_text("history", path, text, f"blob {sha[:10]}")
    return found


def scan(repo: Path, history: bool = True) -> list[Finding]:
    return scan_tree(repo) + (scan_history(repo) if history else [])


def describe_exceptions() -> list[str]:
    return [f"{e.file} ({', '.join(sorted(e.scopes))}) : {e.why}" for e in EXCEPTIONS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.secret_scan", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--no-history", action="store_true", help="arbre de travail seulement")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")                # console Windows : accents corrects
    findings = scan(args.repo, history=not args.no_history)
    print("Exceptions explicites :")
    for line in describe_exceptions():
        print(f"- {line}")
    if findings:
        print(f"ÉCHEC : {len(findings)} secret(s) potentiel(s) :")
        for f in findings:
            print(f"- {f}")
        return 1
    scopes = "arbre de travail" + ("" if args.no_history else " et historique complet")
    print(f"OK : aucun secret ({scopes}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
