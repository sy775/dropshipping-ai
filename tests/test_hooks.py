"""Spec 003, point 5 : scripts de hook, testés comme Claude Code les appelle (JSON sur stdin, code de sortie)."""
import ast
import json
import os
import re
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
    rc, _ = _hook("pre_tool_use.py", {"tool_name": "WebFetch", "tool_input": {"url": "https://example.org/.env"},
                                      "cwd": str(REPO)})
    assert rc == 0


# ---------------------------------------------------------------- pre_tool_use : mention d'un fichier protégé
# Relecture externe de la spec 003 : ces commandes passaient (le hook ne protégeait les fichiers que pour Edit/Write).

GUARDS_ABS = str(REPO / "tests" / "test_audit_guards.py")


@pytest.mark.parametrize("tool,command", [
    # Les cinq commandes de la relecture externe.
    ("Bash", "printf '\\n' >> tests/test_audit_guards.py"),
    ("Bash", "echo KEY=x > .env"),
    ("Bash", "cp /tmp/x tests/test_audit_guards.py"),
    ("Bash", "sed -i 's/a/b/' tests/test_audit_guards.py"),
    ("Bash", "python -c \"open('.env','w').write('x')\""),
    ("PowerShell", "Add-Content tests/test_audit_guards.py x"),
    ("PowerShell", "Set-Content .env x"),
    # Formes de chemin.
    ("Bash", f"cp x '{GUARDS_ABS}'"),
    ("PowerShell", f"Copy-Item x \"{GUARDS_ABS.replace('/', chr(92))}\""),
    ("PowerShell", "Remove-Item .\\tests\\test_audit_guards.py"),
    ("Bash", "cd tests && rm TEST_AUDIT_GUARDS.PY"),
    ("Bash", "rm ./tests/../tests/test_audit_guards.py"),
    ("Bash", "python -c \"p = 'tests/' + 'test_audit_guards.py'\""),
    ("Bash", "git checkout -- tests/test_audit_guards.py"),
    ("Bash", "git add tests/test_audit_guards.py"),
    # Lecture git détournée en écriture.
    ("Bash", "git diff -- tests/test_audit_guards.py > tests/test_audit_guards.py"),
    ("Bash", "git diff --output=x -- tests/test_audit_guards.py"),
    ("Bash", "git -c diff.external=x diff -- tests/test_audit_guards.py"),
    ("Bash", "git log $(rm tests/test_audit_guards.py)"),
    ("PowerShell", "git show HEAD:tests/test_audit_guards.py | Out-File tests/test_audit_guards.py"),
    # .env* : bloqué même en lecture, y compris par git.
    ("Bash", "cat .env"),
    ("Bash", "cat ./.env"),
    ("Bash", "type .ENV.local"),
    ("Bash", "cat config/.env.prod"),
    ("Bash", "cat .envrc"),
    ("Bash", "git show HEAD:.env"),
    ("Bash", "git diff -- .env"),
    ("PowerShell", "Get-Content .env"),
    ("PowerShell", "Get-Content \"config\\.env.local\""),
])
def test_shell_mention_of_protected_file_is_blocked(repo, tool, command):
    _git(repo, "switch", "-q", "-c", "spec/x")
    rc, err = _hook("pre_tool_use.py", _cmd(command, repo, tool))
    assert rc == 2 and "Bloqué" in err, command


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("command", [
    "git diff origin/main...HEAD -- tests/test_audit_guards.py",
    "git diff --quiet origin/main...HEAD -- tests/test_audit_guards.py",
    "git log --oneline -- tests/test_audit_guards.py",
    "git show HEAD:tests/test_audit_guards.py",
    "git status tests/test_audit_guards.py",
    "git --no-pager diff -- tests/test_audit_guards.py",
    "cat .env.example",
    "python -c \"import os; print(os.environ)\"",
    "node -e \"console.log(process.env.HOME)\"",
    "echo $env:PATH",
    "python -m pytest -q",
])
def test_shell_allowed_mentions_pass(repo, tool, command):
    _git(repo, "switch", "-q", "-c", "spec/x")
    rc, err = _hook("pre_tool_use.py", _cmd(command, repo, tool))
    assert (rc, err) == (0, ""), command


# ---------------------------------------------------------------- pre_tool_use : fichiers verrouillés
# Faille trouvée pendant la correction : l'agent pouvait désactiver ses propres garde-fous en modifiant le hook, les
# réglages, le critic, la CI ou l'audit. Une modification voulue se fait par Younes sur une branche human/*.

LOCKED = ("tools/hooks/pre_tool_use.py", "tools/hooks/critic_bash.py", ".claude/settings.json",
          ".claude/agents/critic.md", ".github/workflows/ci.yml", "tools/audit.py",
          # Critic, bloquant 2, et relecture externe : réglages locaux, outils de l'audit, liste partagée.
          ".claude/settings.local.json", "tools/secret_scan.py", "tools/mutation_check.py", "tools/locked_paths.txt")
# Critic, bloquant 4 : dossiers parents des fichiers verrouillés (restauration ou copie du dossier entier).
LOCKED_DIRS = ("tools/", "tools/hooks/", ".claude/", ".claude/agents/", ".github/", ".github/workflows/", "tests/")
LOCKED_LIST = REPO / "tools" / "locked_paths.txt"


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("path", LOCKED)
def test_locked_files_are_blocked_for_write_tools(tool, path):
    for form in (path, str(REPO / path), str(REPO / path).replace("/", "\\"), "./" + path.upper()):
        rc, err = _hook("pre_tool_use.py", _write(form, tool), {"CLAUDE_PROJECT_DIR": str(REPO)})
        assert rc == 2 and "Bloqué" in err, form


@pytest.mark.parametrize("path", LOCKED)
@pytest.mark.parametrize("tool,template", [
    ("Bash", "echo x >> {}"),
    ("Bash", "sed -i 's/a/b/' '{}'"),
    ("Bash", "git checkout origin/main -- {}"),
    ("Bash", "cat {}"),
    ("PowerShell", "Set-Content {} x"),
    ("PowerShell", "Remove-Item \"{}\""),
])
def test_locked_files_are_blocked_for_shell(repo, path, tool, template):
    _git(repo, "switch", "-q", "-c", "spec/x")
    for form in (path, str(REPO / path).replace("/", "\\")):
        rc, err = _hook("pre_tool_use.py", _cmd(template.format(form), repo, tool))
        assert rc == 2 and "Bloqué" in err, template.format(form)


@pytest.mark.parametrize("path", LOCKED)
@pytest.mark.parametrize("template", ["git diff origin/main...HEAD -- {}", "git log --oneline -- {}",
                                      "git show HEAD:{}", "git status {}"])
def test_locked_files_git_read_passes(repo, path, template):
    _git(repo, "switch", "-q", "-c", "spec/x")
    rc, err = _hook("pre_tool_use.py", _cmd(template.format(path), repo))
    assert (rc, err) == (0, "")


def test_locked_list_is_one_file_shared_by_hook_and_audit():
    # Relecture externe : une seule liste, dans un fichier verrouillé, lue par le hook et par le filet.
    from tools import audit
    from tools.hooks import pre_tool_use
    entries = {line.strip() for line in LOCKED_LIST.read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.lstrip().startswith("#")}
    files = {e for e in entries if not e.endswith("/")}
    assert files == set(LOCKED) | {"tests/test_audit_guards.py"}
    assert entries - files == set(LOCKED_DIRS)
    assert set(pre_tool_use.PROTECTED) == set(audit.GUARDED) == files
    assert set(pre_tool_use.PROTECTED_DIRS) == set(LOCKED_DIRS)


# ---------------------------------------------------------------- critic, bloquant 4 : dossiers parents

@pytest.mark.parametrize("tool,command", [
    ("Bash", "git checkout origin/main -- tools/hooks"),
    ("Bash", "git restore --source=origin/main -- .claude"),
    ("Bash", "git checkout HEAD~1 -- .github"),
    ("Bash", "cp -r /tmp/h/. tools/hooks/"),
    ("Bash", "rm -rf .github/workflows"),
    ("Bash", "git checkout HEAD~1 -- tools"),
    ("Bash", "git checkout HEAD~1 -- tests/"),
    ("Bash", "git checkout HEAD~1 -- '.claude/agents'"),
    ("PowerShell", "Copy-Item -Recurse x .claude\\agents"),
    ("PowerShell", f"Remove-Item -Recurse \"{str(REPO / 'tools' / 'hooks')}\""),
])
def test_locked_dirs_are_blocked_for_shell(repo, tool, command):
    _git(repo, "switch", "-q", "-c", "spec/x")
    rc, err = _hook("pre_tool_use.py", _cmd(command, repo, tool))
    assert rc == 2 and "Bloqué" in err, command


@pytest.mark.parametrize("command", [
    "git diff origin/main...HEAD -- tools/hooks",
    "git log --oneline -- .claude",
    "git status .github",
    "git show HEAD -- tests/",
    "python -m tools.audit --protected-only",
    "ls .claude/skills",
    "python -m pytest -q tests/test_hooks.py",
    "cat tools/hooks/stop_pytest.py",
])
def test_locked_dirs_git_read_and_subpaths_pass(repo, command):
    _git(repo, "switch", "-q", "-c", "spec/x")
    assert _hook("pre_tool_use.py", _cmd(command, repo)) == (0, ""), command


# ---------------------------------------------------------------- critic, bloquant 3, et relecture externe : push
# Seul `git push origin spec/<nom>` est permis (avec -u) : rien vers main ni human/* (branche exemptée du filet CI).

@pytest.mark.parametrize("command", [
    "git push origin HEAD:human/x",
    "git push -u origin human/x",
    "git push origin human/x",
    "git push origin HEAD:spec/x",
    "git push origin spec/x:spec/x",
    "git push origin :spec/x",
    "git push origin --delete spec/x",
    "git push -d origin spec/x",
    "git push --force-with-lease origin spec/x",
    "git push -f origin spec/x",
    "git push --force origin spec/x",
    "git push origin +spec/x",
    "git push origin main",
    "git push origin HEAD",
    "git push origin spec/x main",
    "git push origin spec/x spec/y",
    "git push upstream spec/x",
    "git push",
    "git push origin",
    "git push -u origin",
    "git push --tags origin",
    "git push --mirror origin",
])
def test_only_push_of_one_spec_branch_to_origin_is_allowed(repo, command):
    _git(repo, "switch", "-q", "-c", "spec/x")
    rc, err = _hook("pre_tool_use.py", _cmd(command, repo))
    assert rc == 2 and "Bloqué" in err, command


# ---------------------------------------------------------------- critic, bloquant 1 : python -I

def _hook_commands() -> list[str]:
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [h["command"] for group in settings["hooks"].values() for g in group for h in g["hooks"]]
    critic = (REPO / ".claude" / "agents" / "critic.md").read_text(encoding="utf-8")
    commands += [c.strip() for c in re.findall(r"^\s*command:\s*(.+)$", critic, flags=re.M)]
    return commands


def test_every_hook_command_runs_python_isolated():
    commands = _hook_commands()
    assert len(commands) == 3
    for command in commands:
        assert command.startswith("python -I "), command


def test_hook_ignores_module_planted_next_to_it(tmp_path):
    hooks = tmp_path / "tools" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre_tool_use.py").write_bytes((HOOKS / "pre_tool_use.py").read_bytes())
    if LOCKED_LIST.is_file():
        (tmp_path / "tools" / "locked_paths.txt").write_bytes(LOCKED_LIST.read_bytes())
    (hooks / "shlex.py").write_text("import pathlib, sys\n(pathlib.Path(__file__).parent / 'planted').write_text('x')\n"
                                   "sys.exit(0)\n", encoding="utf-8")
    import shlex
    command = next(c for c in _hook_commands() if "pre_tool_use.py" in c)
    args = shlex.split(command.replace("$CLAUDE_PROJECT_DIR", tmp_path.as_posix()))
    assert args[0] == "python"
    payload = json.dumps(_write(".env", "Write", str(tmp_path))).encode("utf-8")
    proc = subprocess.run([sys.executable, *args[1:]], input=payload, capture_output=True,
                          env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)}, timeout=60)
    assert not (hooks / "planted").exists(), "shlex.py déposé à côté du hook a été importé"
    assert proc.returncode == 2


def test_stop_hook_is_not_locked(repo):
    # La spec 004 doit pouvoir modifier stop_pytest.py : il ne protège rien.
    _git(repo, "switch", "-q", "-c", "spec/x")
    assert _hook("pre_tool_use.py", _write("tools/hooks/stop_pytest.py", "Edit"),
                 {"CLAUDE_PROJECT_DIR": str(REPO)}) == (0, "")
    assert _hook("pre_tool_use.py", _cmd("cat tools/hooks/stop_pytest.py", repo)) == (0, "")


# ---------------------------------------------------------------- pre_tool_use : lecture de .env* (Read, Grep, Glob)
# Doc : https://code.claude.com/docs/en/permissions, « What Read and Edit rules cover » : les règles deny Read ne
# s'appliquent à Grep et Glob qu'en « best-effort » ; le hook bloque donc aussi ces outils.

def _tool(tool, **tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": str(REPO)}


@pytest.mark.parametrize("payload", [
    _tool("Read", file_path=".env"),
    _tool("Read", file_path=str(REPO / ".env.local")),
    _tool("Read", file_path="config\\.env.prod"),
    _tool("Grep", pattern="KEY", path=".env"),
    _tool("Grep", pattern="KEY", path="config/.env.local"),
    _tool("Grep", pattern="KEY", glob=".env*"),
    _tool("Grep", pattern="KEY", path=".", glob="**/.env*"),
    _tool("Glob", pattern="**/.env*"),
    _tool("Glob", pattern=".env"),
    _tool("Glob", pattern="*", path="config/.env.d"),
])
def test_reading_env_files_is_blocked(payload):
    rc, err = _hook("pre_tool_use.py", payload, {"CLAUDE_PROJECT_DIR": str(REPO)})
    assert rc == 2 and "Bloqué" in err, payload


@pytest.mark.parametrize("payload", [
    _tool("Read", file_path=".env.example"),
    _tool("Read", file_path="tests/test_audit_guards.py"),
    _tool("Grep", pattern=".env", path="tools"),                          # motif de contenu, pas un chemin
    _tool("Grep", pattern="environ", glob="*.py"),
    _tool("Glob", pattern="**/*.py"),
    _tool("Glob", pattern=".env.example"),
])
def test_other_reads_pass(payload):
    rc, err = _hook("pre_tool_use.py", payload, {"CLAUDE_PROJECT_DIR": str(REPO)})
    assert (rc, err) == (0, ""), payload


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
