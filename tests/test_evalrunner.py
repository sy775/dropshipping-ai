"""Session 2, point 4 : runner d'évaluation (dry-run, aucun appel réel)."""
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from alerts.telegram import MemoryNotifier
from journal import db
from llm import evalrunner
from llm.config import KNOWN_AGENTS, load_prices, load_settings
from llm.evalrunner import EvalError, check, load_cases, main, run_eval, to_markdown, write_report
from llm.pricing import Usage
from llm.providers.base import ProviderResponse
from llm.providers.fake import FakeProvider
from llm.runtime import Runtime
from tests.conftest import NOW, REPO_CONFIG, Clock, edit_yaml

REPO = Path(__file__).resolve().parents[1]
EMAIL = "marie.durand@example.org"


def _runtime(config_dir, live=False, provider=None):
    rt = Runtime(settings=load_settings(config_dir), prices=load_prices(config_dir), conn=db.connect(":memory:"),
                 notifier=MemoryNotifier(), live=live, clock=Clock(NOW), sleep=lambda s: None)
    if provider is not None:
        rt.provider_factory = lambda name: provider
        rt.dry_run_provider = provider
    return rt


def _hash_config():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(REPO_CONFIG.glob("*"))}


# ---------------------------------------------------------------- jeu de cas

@pytest.mark.parametrize("agent", KNOWN_AGENTS)
def test_five_synthetic_cases_per_agent(agent):
    cases = load_cases(agent)
    assert len(cases) == 5
    assert all(c.synthetic for c in cases)
    assert len({c.id for c in cases}) == 5


def test_veille_has_at_least_two_injection_cases():
    assert sum("injection" in c.id for c in load_cases("veille")) >= 2


def test_sav_has_a_client_email_case():
    case = next(c for c in load_cases("sav") if EMAIL in json.dumps(c.messages, ensure_ascii=False))
    assert EMAIL in case.expected["forbidden_values"]


def test_case_schemas_are_structured_output_ready():
    # contrainte de l'API : chaque objet du schéma porte additionalProperties: false
    for agent in KNOWN_AGENTS:
        for c in load_cases(agent):
            if c.schema:
                assert c.schema["additionalProperties"] is False, c.id


# ---------------------------------------------------------------- critères

CONTENT = {"reponse": "Votre colis est en livraison, suivi : 1Z99", "categorie": "livraison", "escalade": False,
           "prix": 21.9}


@pytest.mark.parametrize("expected, n_fail", [
    ({"required_fields": ["reponse", "categorie"]}, 0),
    ({"required_fields": ["absent"]}, 1),
    ({"required_fields": ["reponse"], "forbidden_values": ["AMAZON"]}, 0),
    ({"forbidden_values": ["SUIVI"]}, 1),                         # insensible à la casse
    ({"required_patterns": [r"(?i)suivi"]}, 0),
    ({"required_patterns": [r"remboursement"]}, 1),
    ({"numeric_bounds": {"prix": {"min": 19.9, "max": 24.9}}}, 0),
    ({"numeric_bounds": {"prix": {"max": 20}}}, 1),
    ({"numeric_bounds": {"absent": {"max": 20}}}, 1),
    ({"max_length": 500}, 0),
    ({"max_length": 10}, 1),
])
def test_criteria(expected, n_fail):
    assert len(check(CONTENT, expected)) == n_fail


def test_free_text_criteria_and_no_value_echo():
    failures = check(f"Bonjour {EMAIL}", {"forbidden_values": [EMAIL], "required_patterns": ["^BROUILLON"]})
    assert len(failures) == 2
    assert all(EMAIL not in f for f in failures)


# ---------------------------------------------------------------- runs

def test_report_compares_two_simulated_models(config_dir, tmp_path):
    rt = _runtime(config_dir)
    report = run_eval("sav", ["claude-haiku-5-5", "stub-alt"], runtime=rt)
    assert report["mode"] == "dry-run" and report["indicatif"] is True and report["cost_basis"] == "tarif sync"
    assert set(report["models"]) == {"claude-haiku-5-5", "stub-alt"}
    for s in report["models"].values():
        assert s["executed"] == 5 and s["synthetic_runs"] == 5 and s["median_latency_ms"] is not None
        assert 0 <= s["score"] <= 1
    md, js = write_report(report, tmp_path)
    text = md.read_text(encoding="utf-8")
    assert "INDICATIF" in text and "tarif sync" in text and "stub-alt" in text and "synthétique" in text
    assert json.loads(js.read_text(encoding="utf-8"))["agent"] == "sav"


def test_calls_go_through_adapter_with_purpose_eval(config_dir):
    rt = _runtime(config_dir)
    run_eval("veille", ["claude-sonnet-5-5"], runtime=rt)
    rows = rt.conn.execute("SELECT purpose, mode, dry_run, model FROM llm_calls").fetchall()
    assert len(rows) == 5
    assert all(r["purpose"] == "eval" and r["mode"] == "sync" and r["dry_run"] == 1 for r in rows)
    assert db.month_spend(rt.conn, "2026-11", "prod") == 0


def test_model_substitution_is_in_memory_only(config_dir):
    before = _hash_config()
    rt = _runtime(config_dir)
    run_eval("sav", ["claude-sonnet-5-5", "stub-alt"], runtime=rt)
    assert rt.settings.agents["sav"].model == "claude-haiku-5-5"     # runtime d'origine intact
    assert load_settings(config_dir).agents["sav"].model == "claude-haiku-5-5"
    assert _hash_config() == before                                 # config du dépôt intacte
    models = {r["model"] for r in rt.conn.execute("SELECT model FROM llm_calls")}
    assert models == {"claude-sonnet-5-5", "stub-alt"}


def test_repeat(config_dir):
    report = run_eval("sav", ["stub-alt"], runtime=_runtime(config_dir), repeat=3)
    assert report["models"]["stub-alt"]["runs"] == 15
    with pytest.raises(EvalError):
        run_eval("sav", ["stub-alt"], runtime=_runtime(config_dir), repeat=0)


def test_unknown_model_rejected(config_dir):
    with pytest.raises(EvalError, match="prices.yaml"):
        run_eval("sav", ["gpt-inconnu"], runtime=_runtime(config_dir))


def test_live_max_cost_stops_the_run(config_dir):
    # Réponses au pire cas (max_tokens de sortie, 1 024 pour sav) : chaque appel coûte ~0,0005 $.
    # Plafond du run à 0,0012 $ : le runner s'arrête avant de le dépasser, sur l'estimation de l'appel suivant.
    fake = FakeProvider(responder=lambda req: ProviderResponse(
        text=json.dumps({"reponse": "ok", "categorie": "autre", "escalade": False}),
        usage=Usage(tokens_in=300, tokens_out=req.max_tokens), stop_reason="end_turn"))
    rt = _runtime(config_dir, live=True, provider=fake)
    report = run_eval("sav", ["claude-haiku-5-5"], runtime=rt, live=True, max_cost=0.0012)
    s = report["models"]["claude-haiku-5-5"]
    assert 1 <= s["executed"] <= 2 and s["skipped"] == 5 - s["executed"]
    assert report["spent_usd"] <= 0.0012 and "max-cost" in report["stopped"]
    assert len(fake.requests) == s["executed"]


def test_live_eval_cap_stops_the_run(config_dir):
    edit_yaml(config_dir / "agents.yaml", lambda d: d["budgets"]["eval"].update(global_monthly=0.0001))
    fake = FakeProvider()
    rt = _runtime(config_dir, live=True, provider=fake)
    report = run_eval("sav", ["claude-haiku-5-5"], runtime=rt, live=True)
    assert "plafond" in report["stopped"] and fake.requests == []


def test_failures_are_classified(config_dir):
    outputs = iter([
        ProviderResponse(text="pas du json", usage=Usage(tokens_in=1, tokens_out=1), stop_reason="end_turn"),
        ProviderResponse(text="{}", usage=Usage(tokens_in=1, tokens_out=1), stop_reason="max_tokens"),
        ProviderResponse(text="", usage=Usage(tokens_in=1, tokens_out=1), stop_reason="refusal"),
    ] + [ProviderResponse(text=json.dumps({"reponse": "Suivi en cours, livraison 3 à 5 jours. 30 jours",
                                           "categorie": "livraison", "escalade": True}),
                          usage=Usage(tokens_in=1, tokens_out=1), stop_reason="end_turn")] * 2)
    fake = FakeProvider(responder=lambda req: next(outputs))
    report = run_eval("sav", ["claude-haiku-5-5"], runtime=_runtime(config_dir, provider=fake))
    s = report["models"]["claude-haiku-5-5"]
    assert (s["format_failures"], s["incomplete"], s["refused"]) == (1, 1, 1)


# ---------------------------------------------------------------- données client

def test_client_email_never_reaches_journal_or_reports(config_dir, tmp_path):
    echo = FakeProvider(responder=lambda req: ProviderResponse(   # pire cas : le modèle recopie le message
        text=json.dumps({"reponse": json.dumps(req.messages, ensure_ascii=False), "categorie": "autre",
                         "escalade": False}, ensure_ascii=False), usage=Usage(tokens_in=10, tokens_out=10),
        stop_reason="end_turn"))
    rt = _runtime(config_dir, provider=echo)
    report = run_eval("sav", ["claude-haiku-5-5"], runtime=rt)
    sav3 = next(r for r in report["runs"] if r["case_id"] == "sav-003-email-client")
    assert sav3["status"] == "failed"                                   # le critère a bien détecté l'email
    dump = "\n".join(str(tuple(r)) for t in ("llm_calls", "llm_alerts")
                     for r in rt.conn.execute(f"SELECT * FROM {t}"))
    assert EMAIL not in dump
    md, js = write_report(report, tmp_path)
    assert EMAIL not in md.read_text(encoding="utf-8") and EMAIL not in js.read_text(encoding="utf-8")


# ---------------------------------------------------------------- CLI

def test_cli_dry_run_writes_reports(config_dir, tmp_path, capsys):
    rc = main(["--agent", "sav", "--models", "claude-haiku-5-5,stub-alt", "--out", str(tmp_path)],
              runtime=_runtime(config_dir))
    assert rc == 0
    assert len(list(tmp_path.glob("eval-sav-dry-run-*.md"))) == 1
    assert len(list(tmp_path.glob("eval-sav-dry-run-*.json"))) == 1
    assert "stub-alt" in capsys.readouterr().out


def test_cli_live_requires_llm_live(config_dir, tmp_path, capsys):
    out = tmp_path / "reports"
    rc = main(["--agent", "sav", "--models", "stub-alt", "--live", "--out", str(out)],
              runtime=_runtime(config_dir))
    assert rc == 2 and not out.exists()
    assert "LLM_LIVE=1" in capsys.readouterr().err
