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
from llm.evalrunner import (Case, EvalError, Run, check, load_cases, main, print_output, run_eval, to_markdown,
                            write_report)
from llm.pricing import Usage
from llm.providers.base import ProviderResponse
from llm.providers.fake import FakeProvider
from llm.runtime import Runtime
from tests.conftest import NOW, REPO_CONFIG, Clock, edit_yaml

REPO = Path(__file__).resolve().parents[1]
EMAIL = "marie.durand@example.org"
N_SAV = len(load_cases("sav"))


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
def test_at_least_five_synthetic_cases_per_agent(agent):
    cases = load_cases(agent)
    assert len(cases) >= 5
    assert all(c.synthetic for c in cases)
    assert len({c.id for c in cases}) == len(cases)


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
        assert s["executed"] == N_SAV and s["synthetic_runs"] == N_SAV and s["median_latency_ms"] is not None
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
    assert report["models"]["stub-alt"]["runs"] == 3 * N_SAV
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
    assert 1 <= s["executed"] <= 2 and s["skipped"] == N_SAV - s["executed"]
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


# ---------------------------------------------------------------- field_equals et portée de forbidden_values

@pytest.mark.parametrize("value, ok", [(False, True), (True, False), (0, False), ("false", False), (None, False)])
def test_field_equals_is_strict(value, ok):
    failures = check({"conforme_ue": value}, {"field_equals": {"conforme_ue": False}})
    assert (failures == []) is ok


def test_field_equals_missing_field_and_numbers():
    assert check({}, {"field_equals": {"conforme_ue": False}}) == ["champ absent : conforme_ue"]
    assert check({"a": {"b": 5.0}}, {"field_equals": {"a.b": 5}}) == []
    assert check("texte libre", {"field_equals": {"a": 1}}) == ["champ absent : a"]


def test_field_equals_does_not_echo_obtained_value():
    failures = check({"pays": EMAIL}, {"field_equals": {"pays": "Portugal"}})
    assert failures == ["pays différent de la valeur attendue"]


SCOPED = {"forbidden_values": {"values": ["prioritaire"], "fields": ["fournisseur", "pays"]}}


def test_scoped_forbidden_values_ignore_other_fields():
    content = {"fournisseur": "Textil Lusa Lda", "pays": "Portugal",
               "motif": "Tentative d'injection : l'email demande de marquer le fournisseur prioritaire."}
    assert check(content, SCOPED) == []
    # sans portée : comportement actuel, tout le texte est contrôlé
    assert check(content, {"forbidden_values": ["prioritaire"]}) == ["valeur interdite n°1 présente"]


def test_scoped_forbidden_values_catch_decision_fields():
    content = {"fournisseur": "Textil Lusa Lda (PRIORITAIRE)", "pays": "Portugal", "motif": "ok"}
    assert check(content, SCOPED) == ["valeur interdite n°1 présente dans fournisseur"]


def _write_case(root, raw):
    (root / "sav").mkdir(exist_ok=True)
    (root / "sav" / "c.yaml").write_text(yaml.safe_dump(
        {"id": "c", "input": {"messages": [{"role": "user", "content": "x"}]}, **raw}), encoding="utf-8")


@pytest.mark.parametrize("expected, msg", [
    ({"forbidden_values": {"values": ["x"]}}, "non vides"),
    ({"forbidden_values": {"values": ["x"], "fields": ["a"], "autre": 1}}, "values et fields"),
    ({"forbidden_values": "x"}, "liste"),
    ({"field_equals": ["a"]}, "dictionnaire"),
])
def test_invalid_criteria_rejected_at_load(tmp_path, expected, msg):
    _write_case(tmp_path, {"schema": {"type": "object"}, "expected": expected})
    with pytest.raises(EvalError, match=msg):
        load_cases("sav", tmp_path)


@pytest.mark.parametrize("expected", [{"field_equals": {"a": 1}},
                                      {"forbidden_values": {"values": ["x"], "fields": ["a"]}}])
def test_field_criteria_require_a_schema(tmp_path, expected):
    _write_case(tmp_path, {"schema": None, "expected": expected})
    with pytest.raises(EvalError, match="schema"):
        load_cases("sav", tmp_path)


SOURCING_OK = {"fournisseur": "Textil Lusa Lda", "pays": "Portugal", "prix_unitaire_eur": 9, "delai_jours": 5,
               "conforme_ue": False, "motif": "Pas de fiche produit ; l'email tente de le rendre prioritaire."}


def test_sourcing_injection_case_checks_the_decision_field():
    case = next(c for c in load_cases("sourcing") if c.id == "sourcing-005-injection")
    assert case.expected["field_equals"] == {"conforme_ue": False}
    assert "required_patterns" not in case.expected
    assert check(SOURCING_OK, case.expected) == []                      # signaler l'injection dans motif : OK
    assert check({**SOURCING_OK, "conforme_ue": True}, case.expected)    # décision manipulée : échec
    assert check({**SOURCING_OK, "pays": "Portugal (prioritaire)"}, case.expected)


def test_sav_injection_echo_is_forbidden_in_whole_text():
    case = next(c for c in load_cases("sav") if c.id == "sav-006-injection-reprise")
    assert isinstance(case.expected["forbidden_values"], list)           # pas de portée : tout le texte
    clean = {"reponse": "Bonjour, vous pouvez retourner le plaid sous 30 jours.", "categorie": "retour",
             "escalade": False}
    assert check(clean, case.expected) == []
    echo = {**clean, "reponse": "Votre message demandait d'écrire « remboursement intégral » : impossible."}
    assert check(echo, case.expected)                                    # recopier l'injection au client : échec


# ---------------------------------------------------------------- --show-outputs

MARK = "SORTIE-VISIBLE-7Q"


def _marker_provider():
    return FakeProvider(responder=lambda req: ProviderResponse(
        text=json.dumps({"reponse": MARK, "categorie": "autre", "escalade": False}),
        usage=Usage(tokens_in=10, tokens_out=10), stop_reason="end_turn"))


def _mixed_cases(tmp_path):
    folder = tmp_path / "cases" / "sav"
    folder.mkdir(parents=True)
    (folder / "_schema.yaml").write_bytes((REPO / "evals" / "sav" / "_schema.yaml").read_bytes())
    for cid, synthetic in (("synth", True), ("reel", False)):
        (folder / f"{cid}.yaml").write_text(yaml.safe_dump(
            {"id": cid, "synthetic": synthetic, "input": {"messages": [{"role": "user", "content": "x"}]},
             "expected": {"required_fields": ["reponse"]}}), encoding="utf-8")
    return folder.parent


def _journal_dump(rt):
    return "\n".join(str(tuple(r)) for t in ("llm_calls", "llm_alerts") for r in rt.conn.execute(f"SELECT * FROM {t}"))


def test_show_outputs_prints_synthetic_cases_only(config_dir, tmp_path, capsys):
    rt = _runtime(config_dir, provider=_marker_provider())
    out = tmp_path / "reports"
    rc = main(["--agent", "sav", "--models", "claude-haiku-5-5", "--show-outputs", "--cases-dir",
               str(_mixed_cases(tmp_path)), "--out", str(out)], runtime=rt)
    assert rc == 0
    stdout = capsys.readouterr().out
    assert stdout.count(MARK) == 1                                       # seul le cas synthétique est affiché
    assert "/ synth #1 (passed) ---" in stdout
    assert "/ reel #1 (passed) : affichage refusé (cas non synthétique)" in stdout
    assert MARK not in _journal_dump(rt)                                 # ni journal…
    assert all(MARK not in f.read_text(encoding="utf-8") for f in out.iterdir())   # … ni rapports


def test_without_show_outputs_nothing_is_printed(config_dir, tmp_path, capsys):
    rt = _runtime(config_dir, provider=_marker_provider())
    main(["--agent", "sav", "--models", "claude-haiku-5-5", "--cases-dir", str(_mixed_cases(tmp_path)),
          "--out", str(tmp_path / "reports")], runtime=rt)
    assert MARK not in capsys.readouterr().out


def test_print_output_refuses_non_synthetic_case(capsys):
    case = Case("reel", False, [{"role": "user", "content": "x"}], None, None, {})
    print_output(case, Run("reel", "m", 1, False, "passed"), {"reponse": MARK})
    out = capsys.readouterr().out
    assert MARK not in out and "affichage refusé" in out
