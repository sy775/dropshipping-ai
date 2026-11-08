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


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
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
    (repo / "tools").mkdir()
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
    assert set(pre[0]["matcher"].split("|")) >= {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"}
    assert pre[0]["hooks"][0]["onFailure"] == "block"
    commands = [h["command"] for group in settings["hooks"].values() for g in group for h in g["hooks"]]
    for command in commands:
        script = re.search(r"tools/hooks/\w+\.py", command).group(0)
        assert (REPO / script).is_file()
    assert any("stop_pytest.py" in c for c in commands) and "Stop" in settings["hooks"]
    deny = settings["permissions"]["deny"]
    assert "Edit(tests/test_audit_guards.py)" in deny and "Bash(gh pr merge *)" in deny


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
