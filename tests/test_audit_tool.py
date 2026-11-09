"""Spec 003, point 3 : orchestration de /audit (tools/audit.py) et fichiers de configuration Claude Code."""
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tools import audit

REPO = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.org",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def _seed_locked_list(repo: Path) -> Path:
    """Copie la liste verrouillée du dépôt : sans elle, le filet échoue (contrôle fermé)."""
    target = repo / audit.LOCKED_LIST
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((REPO / audit.LOCKED_LIST).read_bytes())
    return target


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    _seed_locked_list(tmp_path)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "switch", "-q", "-c", "spec/004-essai")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "travail de la spec")
    return tmp_path


class FakeRunner:
    def __init__(self, codes):
        self.codes, self.calls = list(codes), []

    def __call__(self, command, cwd):
        self.calls.append(command)
        rc = self.codes.pop(0)
        return rc, f"sortie de {command[2]} rc={rc}"


def test_steps_run_in_order_and_report_is_written(repo):
    runner = FakeRunner([0, 0, 0])
    assert audit.main([], runner=runner, repo=repo) == 0
    assert [c[2] for c in runner.calls] == ["pytest", "tools.secret_scan", "tools.mutation_check"]
    text = (repo / "docs" / "reports" / "004-essai.md").read_text(encoding="utf-8")
    assert "# Rapport 004-essai" in text and "**OK**" in text and "travail de la spec" in text
    assert text.index("pytest complet") < text.index("secret_scan") < text.index("mutation_check")
    assert "## Critic" in text and "## Hypothèses" in text and "## Écarts avec la documentation officielle" in text


@pytest.mark.parametrize("fail_at", [0, 1, 2])
def test_each_step_is_blocking(repo, fail_at):
    codes = [0] * fail_at + [1]
    runner = FakeRunner(codes)
    assert audit.main([], runner=runner, repo=repo) == 1
    assert len(runner.calls) == fail_at + 1                               # rien après l'échec
    text = (repo / "docs" / "reports" / "004-essai.md").read_text(encoding="utf-8")
    assert "**ÉCHEC**" in text and text.count("| non lancée |") == 2 - fail_at


def test_manual_sections_are_kept_on_rerun(repo):
    audit.main([], runner=FakeRunner([0, 0, 0]), repo=repo)
    path = repo / "docs" / "reports" / "004-essai.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("_À compléter._", "Hypothèse rédigée à la main."), encoding="utf-8")
    audit.main([], runner=FakeRunner([0, 0, 0]), repo=repo)
    assert "Hypothèse rédigée à la main." in path.read_text(encoding="utf-8")


def test_mutation_mode(repo):
    runner = FakeRunner([0, 0, 0])
    audit.main([], runner=runner, repo=repo)                              # catalogue absent de la base : complet
    assert "--changed" not in runner.calls[2]
    (repo / "tools").mkdir(exist_ok=True)
    (repo / "tools" / "mutations.yaml").write_text("[]\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "catalogue")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    runner = FakeRunner([0, 0, 0])
    audit.main([], runner=runner, repo=repo)                              # catalogue inchangé : --changed
    assert "--changed" in runner.calls[2]
    runner = FakeRunner([0, 0, 0])
    audit.main(["--full"], runner=runner, repo=repo)
    assert "--changed" not in runner.calls[2]


# ---------------------------------------------------------------- fichiers protégés (relecture externe)

@pytest.fixture(params=audit.GUARDED)
def guarded(request, tmp_path):
    """Dépôt où le fichier protégé existe sur origin/main ; branche spec/004-essai sans modification."""
    _seed_locked_list(tmp_path)
    path = tmp_path / request.param
    path.parent.mkdir(parents=True, exist_ok=True)
    if request.param != audit.LOCKED_LIST:
        path.write_text("contenu protégé\n", encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "switch", "-q", "-c", "spec/004-essai")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "travail de la spec")
    return tmp_path, path


def test_guarded_untouched_passes(guarded):
    repo, _ = guarded
    assert audit.main(["--protected-only"], repo=repo) == 0
    assert audit.guarded_changes(repo, "origin/main") == []


@pytest.mark.parametrize("commit", [True, False])
@pytest.mark.parametrize("action", ["modifier", "supprimer"])
def test_guarded_change_fails(guarded, commit, action):
    repo, path = guarded
    if action == "modifier":
        path.write_text("affaibli\n", encoding="utf-8")
    else:
        path.unlink()
    if commit:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "affaiblit le garde-fou")
    rel = path.relative_to(repo).as_posix()
    assert audit.guarded_changes(repo, "origin/main") == [rel]
    assert audit.main(["--protected-only"], repo=repo) == 1
    if commit:                                                            # audit complet : rien d'autre n'est lancé
        runner = FakeRunner([0, 0, 0])
        assert audit.main([], runner=runner, repo=repo) == 1 and runner.calls == []
        text = (repo / "docs" / "reports" / "004-essai.md").read_text(encoding="utf-8")
        assert "| fichiers protégés inchangés | échec |" in text and rel in text


def test_guarded_change_on_main_after_branching_is_not_blamed(guarded):
    # origin/main...HEAD : seule la branche compte, pas une modification faite depuis sur main (branche human/*).
    repo, path = guarded
    _git(repo, "switch", "-q", "main")
    path.write_text("modifié par Younes\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "human")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "switch", "-q", "spec/004-essai")
    assert audit.guarded_changes(repo, "origin/main") == []


def test_guarded_first_introduction_is_not_checked(repo):
    for rel in set(audit.GUARDED) - {audit.LOCKED_LIST}:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("nouveau\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "première introduction")
    assert audit.guarded_changes(repo, "origin/main") == []


def test_guarded_list_is_shared_with_the_hook():
    # Relecture externe : une seule liste, lue par le hook et par le filet.
    from tools.hooks import pre_tool_use
    assert set(audit.GUARDED) == set(pre_tool_use.PROTECTED)
    assert audit.LOCKED_LIST in audit.GUARDED                            # la liste est verrouillée elle-même


def test_removing_an_entry_from_the_list_does_not_unlock_it(repo):
    # Le filet lit aussi la liste de la base : retirer une ligne sur la branche ne soustrait pas le fichier.
    target = "tools/secret_scan.py"
    (repo / target).write_text("scan\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "scan")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    listed = repo / audit.LOCKED_LIST
    listed.write_text(listed.read_text(encoding="utf-8").replace(target + "\n", ""), encoding="utf-8")
    (repo / target).write_text("affaibli\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "retire le scan de la liste puis le modifie")
    assert audit.guarded_changes(repo, "origin/main") == [audit.LOCKED_LIST, target]


def test_missing_locked_list_fails(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert audit.main(["--protected-only"], repo=tmp_path) == 1


@pytest.fixture
def catalogue(repo):
    """Catalogue présent sur origin/main (deux entrées) ; branche spec/004-essai."""
    path = repo / audit.CATALOGUE
    path.write_text("- id: m001\n  old: a\n- id: m002\n  old: b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "catalogue")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo, path


def test_catalogue_append_at_end_passes(catalogue):
    repo, path = catalogue
    path.write_text(path.read_text(encoding="utf-8") + "- id: m003\n  old: c\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "ajout")
    assert audit.guarded_changes(repo, "origin/main") == []


@pytest.mark.parametrize("commit", [True, False])
@pytest.mark.parametrize("new_text", [
    "- id: m001\n  old: a\n- id: m002\n  old: B\n",                            # ligne modifiée
    "- id: m001\n  old: a\n",                                                  # entrée supprimée
    "- id: m001\n  old: a\n- id: m009\n  old: z\n- id: m002\n  old: b\n",         # insertion au milieu
])
def test_catalogue_line_of_base_changed_fails(catalogue, commit, new_text):
    repo, path = catalogue
    path.write_text(new_text, encoding="utf-8")
    if commit:
        _git(repo, "commit", "-q", "-am", "modifie le catalogue")
    changed = audit.guarded_changes(repo, "origin/main")
    assert len(changed) == 1 and changed[0].startswith(audit.CATALOGUE)
    assert audit.main(["--protected-only"], repo=repo) == 1


def test_guarded_missing_base_fails(repo):
    assert audit.main(["--protected-only", "--base", "origin/absente"], repo=repo) == 1


def test_audit_refuses_non_spec_branch(repo):
    _git(repo, "switch", "-q", "main")
    with pytest.raises(SystemExit, match="spec/NNN-titre"):
        audit.main([], runner=FakeRunner([0, 0, 0]), repo=repo)


# ---------------------------------------------------------------- configuration Claude Code

def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, path
    return yaml.safe_load(m.group(1))


def test_project_settings_wire_hooks_and_deny_rules():
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    pre = settings["hooks"]["PreToolUse"]
    assert set(pre[0]["matcher"].split("|")) >= {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell",
                                                  "Read", "Grep", "Glob"}
    assert pre[0]["hooks"][0]["onFailure"] == "block"
    commands = [h["command"] for group in settings["hooks"].values() for g in group for h in g["hooks"]]
    for command in commands:
        script = re.search(r"tools/hooks/\w+\.py", command).group(0)
        assert (REPO / script).is_file()
    assert any("stop_pytest.py" in c for c in commands) and "Stop" in settings["hooks"]
    deny = settings["permissions"]["deny"]
    assert "Edit(tests/test_audit_guards.py)" in deny and "Bash(gh pr merge *)" in deny
    assert {"Read(.env*)", "Read(**/.env*)"} <= set(deny)
    assert {f"Edit({path})" for path in audit.GUARDED} <= set(deny)              # fichiers verrouillés


def test_critic_is_read_only():
    fm = _frontmatter(REPO / ".claude" / "agents" / "critic.md")
    tools = {t.strip() for t in fm["tools"].split(",")}
    assert tools == {"Read", "Grep", "Glob", "Bash"}                     # aucun outil d'écriture ni d'édition
    hook = fm["hooks"]["PreToolUse"][0]
    assert "Bash" in hook["matcher"] and "critic_bash.py" in hook["hooks"][0]["command"]


@pytest.mark.parametrize("skill", ["spec", "audit"])
def test_skills_are_user_only(skill):
    fm = _frontmatter(REPO / ".claude" / "skills" / skill / "SKILL.md")
    assert fm["name"] == skill and fm["disable-model-invocation"] is True


def test_spec_skill_stops_before_coding():
    text = (REPO / ".claude" / "skills" / "spec" / "SKILL.md").read_text(encoding="utf-8")
    assert "ARRÊTE-TOI" in text and "origin/main" in text
