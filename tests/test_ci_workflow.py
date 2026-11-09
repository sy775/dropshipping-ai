"""Spec 003, point 6 : workflow CI (YAML valide, déclencheurs, permissions) et notification Telegram."""
import json
from pathlib import Path

import yaml

from alerts.telegram import MemoryNotifier
from tools.ci_notify import build_message, main
from tests.fakes import fake_telegram_token

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _load():
    text = WORKFLOW.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    on = data.get("on", data.get(True))           # PyYAML (YAML 1.1) lit la clé `on` comme le booléen True
    return text, data, on


def test_triggers():
    text, _, on = _load()
    assert set(on) == {"push", "schedule", "workflow_dispatch"}
    assert on["push"]["branches"] == ["**"]
    assert len(on["schedule"]) == 1 and on["schedule"][0]["cron"].split()[2:] == ["*", "*", "*"]   # quotidien
    assert "pull_request_target" not in text


def test_read_only_permissions_everywhere():
    _, data, _ = _load()
    assert data["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in data["jobs"].values())


def test_jobs_python_and_cache():
    _, data, _ = _load()
    assert set(data["jobs"]) == {"pytest", "secret_scan", "mutation_check", "protected_files", "notify"}
    for job in data["jobs"].values():
        setup = [s for s in job["steps"] if s.get("uses", "").startswith("actions/setup-python")]
        assert setup and setup[0]["with"] == {"python-version": "3.12", "cache": "pip"}


def _checkout(job):
    return next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout"))


def test_secret_scan_and_mutations_use_full_history():
    _, data, _ = _load()
    jobs = data["jobs"]
    assert _checkout(jobs["secret_scan"])["with"]["fetch-depth"] == 0
    assert any(s.get("run") == "python -m tools.secret_scan" for s in jobs["secret_scan"]["steps"])
    assert _checkout(jobs["mutation_check"])["with"]["fetch-depth"] == 0


def test_mutation_modes():
    _, data, _ = _load()
    steps = {s.get("name"): s for s in data["jobs"]["mutation_check"]["steps"]}
    full, changed = steps["Mutations (complet)"], steps["Mutations (--changed)"]
    assert "--changed" not in full["run"] and "refs/heads/main" in full["if"] and "event_name != 'push'" in full["if"]
    assert "--changed" in changed["run"] and "refs/heads/spec/" in changed["if"]


def test_protected_files_job_runs_everywhere_but_main_and_human():
    # Relecture externe de la spec 003 : filet hors agent pour les fichiers protégés.
    _, data, _ = _load()
    job = data["jobs"]["protected_files"]
    assert job["if"] == "github.ref != 'refs/heads/main' && !startsWith(github.ref, 'refs/heads/human/')"
    assert _checkout(job)["with"]["fetch-depth"] == 0
    assert any(s.get("run") == "python -m tools.audit --protected-only --base origin/main" for s in job["steps"])


def test_notify_is_skipped_without_secrets_and_never_echoes_them():
    text, data, _ = _load()
    notify = data["jobs"]["notify"]
    assert notify["if"] == "always()" and set(notify["needs"]) == {"pytest", "secret_scan", "mutation_check",
                                                                   "protected_files"}
    step = next(s for s in notify["steps"] if s.get("name") == "Notification Telegram")
    assert "env.TELEGRAM_BOT_TOKEN != ''" in step["if"] and "env.TELEGRAM_CHAT_ID != ''" in step["if"]
    assert step["run"] == "python -m tools.ci_notify"
    # Les secrets n'apparaissent que dans des blocs env, jamais dans une commande.
    assert all("secrets." not in s.get("run", "") for j in data["jobs"].values() for s in j["steps"])
    assert text.count("secrets.") == 2


def test_notification_message_and_redaction():
    env = {"GITHUB_REPOSITORY": "sy775/dropshipping-ai", "GITHUB_REF_NAME": "spec/003-outillage",
           "GITHUB_EVENT_NAME": "push", "GITHUB_RUN_ID": "42", "GITHUB_SERVER_URL": "https://github.com",
           "NEEDS_JSON": json.dumps({"pytest": {"result": "success"}, "secret_scan": {"result": "failure"},
                                     "mutation_check": {"result": "skipped"}}),
           "TELEGRAM_BOT_TOKEN": fake_telegram_token(), "TELEGRAM_CHAT_ID": "1"}
    notifier = MemoryNotifier()
    assert main(notifier=notifier, env=env) == 0
    msg = notifier.messages[0]
    assert msg.startswith("CI ÉCHEC") and "secret_scan failure" in msg and "actions/runs/42" in msg
    assert fake_telegram_token() not in msg
    ok = build_message({"pytest": {"result": "success"}, "mutation_check": {"result": "skipped"}}, env)
    assert ok.startswith("CI OK")
