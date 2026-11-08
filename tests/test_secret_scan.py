"""Spec 003, point 3 : scan de secrets (arbre et historique), exceptions explicites."""
import subprocess
from pathlib import Path

import pytest

from tools import secret_scan
from tools.secret_scan import REPO_ROOT, main, scan
from tests.fakes import fake_anthropic_key, fake_bearer, fake_telegram_token

KEY = fake_anthropic_key()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.org",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README.md").write_text("dépôt de test\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, msg: str = "c") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def test_fake_key_in_tree_is_detected_and_never_printed(repo, capsys):
    _write(repo, "app/config.py", f'API = "{KEY}"\n')                     # non suivi, non ignoré
    findings = scan(repo)
    assert [(f.scope, f.path, f.line_no) for f in findings] == [("tree", "app/config.py", 1)]
    assert main(["--repo", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "ÉCHEC" in out and KEY not in out


def test_fake_key_removed_from_tree_is_still_found_in_history(repo):
    _write(repo, "app/config.py", f'API = "{KEY}"\n')
    _commit(repo)
    (repo / "app" / "config.py").unlink()
    _commit(repo, "retrait")
    findings = scan(repo)
    assert [(f.scope, f.path) for f in findings] == [("history", "app/config.py")]
    assert scan(repo, history=False) == []


def test_telegram_token_and_other_branch_history(repo):
    _git(repo, "switch", "-q", "-c", "autre")
    _write(repo, "notes.txt", f"tg={fake_telegram_token()}\n")
    _commit(repo)
    _git(repo, "switch", "-q", "main")
    assert [(f.scope, f.path, f.pattern_no) for f in scan(repo)] == [("history", "notes.txt", 3)]


def test_ignored_env_file_is_not_read(repo):
    _write(repo, ".gitignore", ".env\n")
    _write(repo, ".env", f"ANTHROPIC_API_KEY={KEY}\n")
    assert scan(repo) == []


def test_redact_regex_is_the_only_tree_exception(repo):
    line = f'    re.compile(r"{KEY}"),\n'                                  # ligne au format des regex de redact.py
    _write(repo, "journal/redact.py", line)
    _commit(repo)
    assert scan(repo) == []                                                # exception : arbre et historique
    _write(repo, "journal/autre.py", line)                                 # même ligne, autre fichier
    assert [f.path for f in scan(repo)] == ["journal/autre.py"]


def test_redact_exception_does_not_cover_a_key_after_the_regex(repo):
    # Critic, point bloquant 2 : seule la regex elle-même est exemptée, pas le reste de la ligne.
    _write(repo, "journal/redact.py", f'    re.compile(r"x"),  # {KEY}\n')
    assert [(f.scope, f.path) for f in scan(repo, history=False)] == [("tree", "journal/redact.py")]


def test_bearer_exception_is_history_only_and_file_specific(repo):
    line = f'    text = f"key=x {fake_bearer()}"\n'
    _write(repo, "tests/test_alerts.py", line)
    _commit(repo)
    findings = scan(repo)
    assert [(f.scope, f.path) for f in findings] == [("tree", "tests/test_alerts.py")]   # jamais dans l'arbre
    (repo / "tests" / "test_alerts.py").write_text("# nettoyé\n", encoding="utf-8")
    _commit(repo)
    assert scan(repo) == []                                                # historique : exception
    _write(repo, "tests/test_autre.py", line)
    _commit(repo)
    (repo / "tests" / "test_autre.py").unlink()
    _commit(repo)
    assert [(f.scope, f.path) for f in scan(repo)] == [("history", "tests/test_autre.py")]


def test_exceptions_are_explicit_and_limited():
    assert {e.file for e in secret_scan.EXCEPTIONS} == {"journal/redact.py", "tests/test_alerts.py"}
    assert {e.file: e.scopes for e in secret_scan.EXCEPTIONS}["tests/test_alerts.py"] == {"history"}


@pytest.mark.skipif(not (REPO_ROOT / ".git").exists(), reason="copie sans .git (contrôle de mutations)")
def test_repository_is_clean():
    # En CI (clone superficiel), seul l'historique disponible est lu ; le job secret_scan prend tout l'historique.
    assert [str(f) for f in scan(REPO_ROOT)] == []
