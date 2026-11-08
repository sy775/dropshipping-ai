"""Spec 003, point 5 : scripts de hook, testés comme Claude Code les appelle (JSON sur stdin, code de sortie)."""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "tools" / "hooks"


def _hook(script: str, payload, env_extra: dict | None = None, raw: bytes | None = None):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env.update(env_extra or {})
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    proc = subprocess.run([sys.executable, str(HOOKS / script)], input=data, capture_output=True, env=env,
                          timeout=120)
    return proc.returncode, proc.stderr.decode("utf-8")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.org",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("X = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


# ---------------------------------------------------------------- pre_tool_use : fichiers

def _write(path, tool="Write", cwd=None):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {key: path}, "cwd": cwd or str(REPO)}


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
@pytest.mark.parametrize("path", [
    "tests/test_audit_guards.py",
    str(REPO / "tests" / "test_audit_guards.py"),
    str(REPO / "tests" / "test_audit_guards.py").replace("/", "\\"),
    "./tests/../tests/TEST_AUDIT_GUARDS.py",
    ".env",
    "config/.env.local",
    str(REPO / ".env.production"),
])
def test_protected_files_are_blocked(tool, path):
    rc, err = _hook("pre_tool_use.py", _write(path, tool), {"CLAUDE_PROJECT_DIR": str(REPO)})
    assert rc == 2 and "Bloqué" in err


@pytest.mark.parametrize("path", [".env.example", "tests/test_hooks.py", "tools/mutations.yaml", "docs/env.md",
                                  "llm/adapter.py"])
def test_other_files_are_allowed(path):
    rc, err = _hook("pre_tool_use.py", _write(path, "Edit"), {"CLAUDE_PROJECT_DIR": str(REPO)})
    assert (rc, err) == (0, "")


# ---------------------------------------------------------------- pre_tool_use : commandes

def _cmd(command, cwd, tool="Bash"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("command", [
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin spec/x:refs/heads/main",
    "git -C . push origin main",
    "git push --force origin spec/x",
    "git push --force-with-lease origin spec/x",
    "git push -f origin spec/x",
    "git push origin +spec/x",
    "git push --all origin",
    "git status && git push origin main",
    "gh repo edit --visibility public",
    "gh secret set TOKEN",
    "git filter-repo --path .env --invert-paths",
    "gh pr merge 12 --squash",
    "rm --force fichier",
])
def test_forbidden_commands_are_blocked(repo, tool, command):
    _git(repo, "switch", "-q", "-c", "spec/x")
    rc, err = _hook("pre_tool_use.py", _cmd(command, repo, tool))
    assert rc == 2 and "Bloqué" in err


@pytest.mark.parametrize("command", ["git push", "git push origin", "git push origin HEAD", "git push -u origin"])
def test_push_without_refspec_from_main_is_blocked(repo, command):
    rc, err = _hook("pre_tool_use.py", _cmd(command, repo))
    assert rc == 2 and "main" in err


@pytest.mark.parametrize("command", ["git switch main && git push", "git checkout main; git push origin HEAD",
                                     "git switch -q main && git push -u origin"])
def test_push_after_switching_to_main_is_blocked(repo, command):
    # Critic, point bloquant 1 : la branche courante change au sein de la même commande.
    _git(repo, "switch", "-q", "-c", "spec/x")
    rc, err = _hook("pre_tool_use.py", _cmd(command, repo))
    assert rc == 2 and "main" in err


@pytest.mark.parametrize("command", [
    "git push origin spec/xxx",
    "git push -u origin spec/003-outillage",
    "git push",                                          # branche courante spec/x
    "git status",
    "git log --oneline -3",
    ".venv/Scripts/python.exe -m pytest -q",
])
def test_allowed_commands_pass(repo, command):
    _git(repo, "switch", "-q", "-c", "spec/x")
    rc, err = _hook("pre_tool_use.py", _cmd(command, repo))
    assert (rc, err) == (0, "")


def test_unreadable_input_is_blocked():
    rc, err = _hook("pre_tool_use.py", None, raw=b"pas du json")
    assert rc == 2 and "illisible" in err


def test_other_tools_are_ignored():
    rc, _ = _hook("pre_tool_use.py", {"tool_name": "Read", "tool_input": {"file_path": ".env"}, "cwd": str(REPO)})
    assert rc == 0


# ---------------------------------------------------------------- critic_bash

@pytest.mark.parametrize("command", ["git diff origin/main...HEAD", "git log --oneline origin/main..HEAD",
                                     "pytest -q", "python -m pytest -x -q", ".venv/Scripts/python.exe -m pytest"])
def test_critic_allowed(command):
    assert _hook("critic_bash.py", {"tool_name": "Bash", "tool_input": {"command": command}})[0] == 0


@pytest.mark.parametrize("command", ["git commit -m x", "git push origin spec/x", "git diff > diff.txt",
                                     "git diff --output=diff.txt", "git diff --ext-diff", "git log; rm -rf .",
                                     "cat .env", "python -c 'print(1)'", "echo $(git diff)", "pytest && git push",
                                     "git checkout main", ""])
def test_critic_blocked(command):
    rc, err = _hook("critic_bash.py", {"tool_name": "Bash", "tool_input": {"command": command}})
    assert rc == 2 and "critic" in err


# ---------------------------------------------------------------- stop_pytest

@pytest.fixture
def project(repo):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    return repo


def _stop(project, active=False, python=None):
    env = {"CLAUDE_PROJECT_DIR": str(project)}
    if python:
        env["CLAUDE_STOP_PYTEST_PYTHON"] = python
    return _hook("stop_pytest.py", {"hook_event_name": "Stop", "stop_hook_active": active, "cwd": str(project)}, env)


def test_stop_green_records_stamp_then_skips(project):
    assert _stop(project) == (0, "")
    assert (project / ".pytest_cache" / "claude-stop-green").is_file()
    assert _stop(project, python="python-introuvable") == (0, "")     # rien de changé : pytest pas relancé


def test_stop_red_blocks_with_feedback(project):
    (project / "tests" / "test_ko.py").write_text("def test_ko():\n    assert 1 == 2\n", encoding="utf-8")
    rc, err = _stop(project)
    assert rc == 2 and "pytest -x -q échoue" in err and "test_ko" in err


def test_stop_reruns_after_python_change(project):
    assert _stop(project)[0] == 0
    (project / "tests" / "test_ok.py").write_text("def test_ok():\n    assert False\n", encoding="utf-8")
    assert _stop(project)[0] == 2


def test_stop_hook_active_never_blocks(project):
    (project / "tests" / "test_ko.py").write_text("def test_ko():\n    assert False\n", encoding="utf-8")
    assert _stop(project, active=True) == (0, "")


# ---------------------------------------------------------------- contraintes générales

@pytest.mark.parametrize("script", sorted(p.name for p in HOOKS.glob("*.py") if p.name != "__init__.py"))
def test_hooks_use_standard_library_only(script):
    tree = ast.parse((HOOKS / script).read_text(encoding="utf-8"))
    modules = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    modules |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert modules - {"__future__"} <= set(sys.stdlib_module_names)
