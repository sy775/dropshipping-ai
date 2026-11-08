"""Spec 003, point 3 : contrôle de mutations, sur un mini-dépôt temporaire (rapide, sans le vrai catalogue)."""
import subprocess
from pathlib import Path

import pytest
import yaml

from tools import mutation_check
from tools.mutation_check import main, run, to_markdown

CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b  # soustraction\n"
TEST = "from calc import add, sub\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
KILLED = {"id": "m001", "description": "addition cassée", "file": "calc.py", "old": "return a + b", "new": "return a * b + 1"}
SURVIVES = {"id": "m002", "description": "soustraction cassée (non testée)", "file": "calc.py",
            "old": "return a - b", "new": "return a + b + 0"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.org",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


def _catalogue(repo: Path, entries: list[dict]) -> None:
    (repo / "tools").mkdir(exist_ok=True)
    (repo / "tools" / "mutations.yaml").write_text(yaml.safe_dump(entries, allow_unicode=True, sort_keys=False),
                                                   encoding="utf-8")


def _commit(repo: Path, msg: str = "c") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    """Mini-dépôt : `main` publié comme origin/main, sans catalogue ; branche spec/x en cours."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "calc.py").write_text(CALC, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(TEST, encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\npythonpath = .\n", encoding="utf-8")
    _commit(tmp_path, "init")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "switch", "-q", "-c", "spec/x")
    return tmp_path


def _publish_main(repo: Path) -> None:
    """Simule la fusion : origin/main pointe sur HEAD (catalogue compris)."""
    _commit(repo, "catalogue")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def test_killed_mutation_passes_and_working_tree_is_untouched(repo):
    _catalogue(repo, [KILLED])
    report = run(repo, jobs=1)
    assert report.ok, to_markdown(report)
    assert [(o.id, o.status) for o in report.outcomes] == [("m001", "killed")]
    assert (repo / "calc.py").read_text(encoding="utf-8") == CALC
    assert any("première introduction" in n for n in report.notes)


def test_surviving_mutation_fails(repo):
    _catalogue(repo, [KILLED, SURVIVES])
    report = run(repo, jobs=2)
    assert not report.ok
    assert {o.id: o.status for o in report.outcomes} == {"m001": "killed", "m002": "survived"}
    assert "m002 (survived)" in to_markdown(report)


@pytest.mark.parametrize("old, count", [("return a / b", 0), ("return a", 2)])
def test_old_not_present_exactly_once_fails(repo, old, count):
    _catalogue(repo, [{**KILLED, "old": old}])
    report = run(repo, jobs=1)
    assert not report.ok and report.outcomes == []                       # rien n'est exécuté
    assert f"`old` présent {count} fois" in report.errors[0]


def test_catalogue_missing_from_branch_fails(repo):
    report = run(repo, jobs=1)
    assert not report.ok and "absent de la branche" in report.errors[0]


def test_duplicate_id_and_noop_mutation_fail(repo):
    _catalogue(repo, [KILLED, {**SURVIVES, "id": "m001"}, {**SURVIVES, "id": "m003", "new": SURVIVES["old"]}])
    errors = " | ".join(run(repo, jobs=1).errors)
    assert "m001 : id en double" in errors and "m003 : new identique à old" in errors


def test_unknown_base_ref_fails(repo):
    _catalogue(repo, [KILLED])
    report = run(repo, base="origin/inexistante", jobs=1)
    assert not report.ok and "référence de base introuvable" in report.errors[0]


def test_no_first_introduction_exemption_once_catalogue_is_on_main(repo):
    _catalogue(repo, [KILLED, SURVIVES])
    _publish_main(repo)
    _catalogue(repo, [KILLED])                                          # m002 supprimée
    report = run(repo, jobs=1)
    assert not report.ok
    assert "m002 : entrée de origin/main supprimée ou id modifié" in report.errors[0]
    assert not any("première introduction" in n for n in report.notes)
    _catalogue(repo, [KILLED, {**SURVIVES, "id": "m020"}])               # id changé
    assert "m002 : entrée" in run(repo, jobs=1).errors[0]


def test_modified_entries_are_listed_before_after(repo):
    _catalogue(repo, [KILLED])
    _publish_main(repo)
    _catalogue(repo, [{**KILLED, "new": "return a - b"}])
    report = run(repo, jobs=1)
    assert report.ok
    md = to_markdown(report)
    assert "Entrées modifiées (avant / après)" in md and "'return a * b + 1'" in md and "'return a - b'" in md


def test_only_and_changed_selection(repo):
    _catalogue(repo, [KILLED, SURVIVES])
    _publish_main(repo)
    assert [o.id for o in run(repo, only=["m001"], jobs=1).outcomes] == ["m001"]
    assert "id inconnus m999" in run(repo, only=["m999"], jobs=1).errors[0]
    assert run(repo, changed=True, jobs=1).selected == 0                 # rien de modifié depuis main
    (repo / "calc.py").write_text(CALC + "\n# modif\n", encoding="utf-8")
    assert run(repo, changed=True, jobs=1).selected == 2                 # fichier des deux mutations modifié


def test_red_suite_without_mutation_fails(repo):
    (repo / "tests" / "test_calc.py").write_text(TEST + "\n\ndef test_rouge():\n    assert False\n",
                                                 encoding="utf-8")
    _catalogue(repo, [KILLED])
    report = run(repo, jobs=1)
    assert not report.ok and "sans mutation" in report.errors[0]


def test_cli_exit_codes(repo, capsys):
    _catalogue(repo, [KILLED])
    assert main(["--repo", str(repo), "--jobs", "1"]) == 0
    assert "**OK** : 1 / 1" in capsys.readouterr().out
    _catalogue(repo, [KILLED, SURVIVES])
    assert main(["--repo", str(repo), "--jobs", "1", "--only", "m002"]) == 1


def test_real_catalogue_matches_the_code():
    # Contrôle rapide du vrai catalogue (sans exécuter les mutations) : structure et `old` uniques.
    report = mutation_check.Report()
    entries = yaml.safe_load((mutation_check.REPO_ROOT / mutation_check.CATALOGUE).read_text(encoding="utf-8"))
    mutation_check.validate(entries, mutation_check.REPO_ROOT, report)
    assert report.errors == [] and len(entries) >= 91
