"""Runner d'évaluation : rejoue les cas evals/<agent>/*.yaml sur un ou plusieurs modèles et écrit un rapport.

    python -m llm.evalrunner --agent sav --models claude-haiku-5-5,stub-alt [--live] [--repeat N] [--max-cost 1]
                             [--show-outputs]

- Dry-run par défaut (fournisseur simulé). --live n'a d'effet qu'avec LLM_LIVE=1 dans l'environnement.
- Tous les appels passent par l'adaptateur, en mode sync, avec purpose="eval" (plafond eval de agents.yaml).
- Critères vérifiables par code uniquement : champs requis, valeurs exactes de champs, valeurs interdites
  (dans tout le texte ou dans certains champs), motifs requis, bornes numériques, longueur, conformité au schéma.
- --show-outputs affiche dans la console la sortie des cas synthétiques uniquement (jamais journal ni rapport).
- Le runner ne modifie jamais la configuration : le modèle évalué est substitué dans une copie en mémoire.
  Le rapport est indicatif ; la décision de changer de modèle reste humaine.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from journal.redact import redact
from llm import adapter
from llm.adapter import LLMCallError, LLMFormatError, LLMIncompleteError, LLMRefusedError
from llm.config import ConfigError, Settings
from llm.guards import BudgetExceeded, GuardError
from llm.pricing import estimate_cost, estimate_raw_tokens, payload_chars, resolve_price
from llm.runtime import REPO_ROOT, Runtime

EVALS_DIR = REPO_ROOT / "evals"
REPORTS_DIR = REPO_ROOT / "data" / "reports"
DEFAULT_MAX_COST = 1.0
NOTICE = ("Rapport indicatif : coût au tarif sync (les agents en lot coûteraient environ moitié moins), "
          "critères vérifiables par code uniquement, cas synthétiques signalés. Aucune configuration n'est "
          "modifiée ; la décision reste humaine.")


class EvalError(Exception):
    pass


# ====================================================================== cas

@dataclass(frozen=True)
class Case:
    id: str
    synthetic: bool
    messages: list[dict]
    cache_prefix: str | None
    schema: dict | None
    expected: dict[str, Any]


def load_cases(agent: str, cases_dir: Path | None = None) -> list[Case]:
    """Charge evals/<agent>/*.yaml. `_schema.yaml` (optionnel) est le schéma par défaut de l'agent ;
    un cas peut le remplacer (`schema: ...`) ou s'en passer (`schema: null`)."""
    folder = Path(cases_dir or EVALS_DIR) / agent
    if not folder.is_dir():
        raise EvalError(f"aucun cas pour l'agent {agent!r} ({folder})")
    default_schema = None
    if (folder / "_schema.yaml").is_file():
        default_schema = yaml.safe_load((folder / "_schema.yaml").read_text(encoding="utf-8"))
    cases = []
    for path in sorted(folder.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        where = f"{agent}/{path.name}"
        if not isinstance(raw, dict) or "id" not in raw or "input" not in raw:
            raise EvalError(f"{where} : champs id et input obligatoires")
        messages = raw["input"].get("messages")
        if not isinstance(messages, list) or not messages:
            raise EvalError(f"{where} : input.messages doit être une liste non vide")
        case = Case(
            id=str(raw["id"]),
            synthetic=bool(raw.get("synthetic", False)),
            messages=messages,
            cache_prefix=raw["input"].get("cache_prefix"),
            schema=raw["schema"] if "schema" in raw else default_schema,
            expected=raw.get("expected") or {},
        )
        _validate_expected(case, where)
        cases.append(case)
    if not cases:
        raise EvalError(f"aucun cas dans {folder}")
    return cases


def _forbidden(expected: dict[str, Any]) -> tuple[list, list[str] | None]:
    """`forbidden_values` : liste (tout le texte) ou {values: [...], fields: [...]} (champs ciblés)."""
    spec = expected.get("forbidden_values") or []
    if isinstance(spec, dict):
        return list(spec.get("values") or []), list(spec.get("fields") or [])
    return list(spec), None


def _validate_expected(case: Case, where: str) -> None:
    exp = case.expected
    spec = exp.get("forbidden_values")
    if isinstance(spec, dict):
        if set(spec) - {"values", "fields"}:
            raise EvalError(f"{where} : forbidden_values n'accepte que values et fields")
        values, fields = _forbidden(exp)
        if not values or not fields:
            raise EvalError(f"{where} : forbidden_values.values et forbidden_values.fields doivent être non vides")
        if case.schema is None:
            raise EvalError(f"{where} : forbidden_values.fields exige une sortie structurée (schema)")
    elif spec is not None and not isinstance(spec, list):
        raise EvalError(f"{where} : forbidden_values doit être une liste ou {{values, fields}}")
    eq = exp.get("field_equals")
    if eq is not None:
        if not isinstance(eq, dict) or not eq:
            raise EvalError(f"{where} : field_equals doit être un dictionnaire non vide {{champ: valeur}}")
        if case.schema is None:
            raise EvalError(f"{where} : field_equals exige une sortie structurée (schema)")


# ====================================================================== critères (code uniquement)

def _same(value: Any, wanted: Any) -> bool:
    """Égalité stricte : False ne vaut pas 0, "1" ne vaut pas 1 ; 5 et 5.0 sont égaux."""
    if isinstance(value, bool) or isinstance(wanted, bool):
        return isinstance(value, bool) and isinstance(wanted, bool) and value is wanted
    if isinstance(value, (int, float)) and isinstance(wanted, (int, float)):
        return value == wanted
    return type(value) is type(wanted) and value == wanted


def _get_path(obj: Any, path: str) -> tuple[bool, Any]:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def check(content: Any, expected: dict[str, Any]) -> list[str]:
    """Renvoie la liste des critères en échec (vide = cas réussi). Messages masqués (aucune donnée client)."""
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    failures: list[str] = []
    for path in expected.get("required_fields", []):
        found, value = _get_path(content, path) if isinstance(content, dict) else (False, None)
        if not found or value in (None, "", [], {}):
            failures.append(f"champ requis absent ou vide : {path}")
    for path, wanted in (expected.get("field_equals") or {}).items():
        found, value = _get_path(content, path) if isinstance(content, dict) else (False, None)
        if not found:
            failures.append(f"champ absent : {path}")
        elif not _same(value, wanted):
            failures.append(f"{path} différent de la valeur attendue")    # la valeur obtenue n'est pas recopiée
    values, fields = _forbidden(expected)
    if fields is None:
        scopes = [("", text)]
    else:
        scopes = []
        for path in fields:
            found, value = _get_path(content, path) if isinstance(content, dict) else (False, None)
            if found and value is not None:
                scopes.append((f" dans {path}", value if isinstance(value, str)
                               else json.dumps(value, ensure_ascii=False)))
    for i, value in enumerate(values):
        for where, scope in scopes:
            if str(value).lower() in scope.lower():
                failures.append(f"valeur interdite n°{i + 1} présente{where}")   # la valeur n'est pas recopiée
    for pattern in expected.get("required_patterns", []):
        if not re.search(pattern, text):
            failures.append(f"motif requis absent : {pattern}")
    for path, bounds in (expected.get("numeric_bounds") or {}).items():
        found, value = _get_path(content, path) if isinstance(content, dict) else (False, None)
        if not found or isinstance(value, bool) or not isinstance(value, (int, float)):
            failures.append(f"valeur numérique absente : {path}")
            continue
        if "min" in bounds and value < bounds["min"]:
            failures.append(f"{path} = {value} < {bounds['min']}")
        if "max" in bounds and value > bounds["max"]:
            failures.append(f"{path} = {value} > {bounds['max']}")
    max_len = expected.get("max_length")
    if max_len is not None and len(text) > max_len:
        failures.append(f"longueur {len(text)} > {max_len}")
    return [redact(f) for f in failures]


# ====================================================================== exécution

@dataclass
class Run:
    case_id: str
    model: str
    repeat: int
    synthetic: bool
    status: str                     # passed, failed, format_error, incomplete, refused, error, skipped
    failures: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    latency_ms: int | None = None
    call_id: str | None = None


def _model_runtime(rt: Runtime, agent: str, model: str, live: bool) -> Runtime:
    """Copie en mémoire du runtime avec le modèle évalué. Aucun fichier de configuration n'est touché."""
    price = rt.prices.models.get(model)
    if price is None:
        raise EvalError(f"modèle inconnu de prices.yaml : {model!r} (fournisseur indéterminé)")
    cfg = dataclasses.replace(rt.settings.agent(agent), provider=price.provider, model=model)
    settings: Settings = dataclasses.replace(rt.settings, agents={**rt.settings.agents, agent: cfg})
    return dataclasses.replace(rt, settings=settings, live=live, _providers={})


def _estimate(rt: Runtime, agent: str, case: Case) -> float:
    cfg = rt.settings.agent(agent)
    price = resolve_price(rt.prices, cfg.model, rt.clock().date()).price
    cpt = rt.settings.chars_per_token
    raw = estimate_raw_tokens(payload_chars(case.messages, case.cache_prefix, case.schema), cpt)
    prefix = estimate_raw_tokens(len(case.cache_prefix), cpt) if case.cache_prefix else 0
    return estimate_cost(price, raw, cfg.max_tokens, rt.settings.input_margin, cached_prefix_tokens=prefix).cost_usd


def _row(rt: Runtime, call_id: str | None):
    if not call_id:
        return None
    return rt.conn.execute("SELECT cost_usd, latency_ms FROM llm_calls WHERE call_id = ?", (call_id,)).fetchone()


OutputHook = Callable[[Case, "Run", Any], None]


def run_eval(agent: str, models: list[str], *, runtime: Runtime, live: bool = False, repeat: int = 1,
             max_cost: float = DEFAULT_MAX_COST, cases_dir: Path | None = None,
             on_output: OutputHook | None = None) -> dict[str, Any]:
    """`on_output(case, run, content)` reçoit la sortie de chaque appel abouti ; elle n'entre jamais dans
    le rapport ni dans le journal."""
    if repeat < 1:
        raise EvalError("--repeat doit être >= 1")
    cases = load_cases(agent, cases_dir)
    runs: list[Run] = []
    spent = 0.0
    stop_reason: str | None = None
    for model in models:
        rt = _model_runtime(runtime, agent, model, live)
        for r in range(1, repeat + 1):
            for case in cases:
                run = Run(case.id, model, r, case.synthetic, "skipped")
                runs.append(run)
                if stop_reason:
                    run.failures = [stop_reason]
                    continue
                if live and spent + _estimate(rt, agent, case) > max_cost:
                    stop_reason = f"arrêt : --max-cost {max_cost:g} $ atteint ({spent:.4f} $ dépensés)"
                    run.failures = [stop_reason]
                    continue
                content = _execute(rt, agent, case, run)
                if on_output is not None and run.status in ("passed", "failed"):
                    on_output(case, run, content)
                spent += run.cost_usd if live else 0.0
                if run.status == "error" and run.failures and run.failures[0].startswith("plafond"):
                    stop_reason = f"arrêt : {run.failures[0]}"
    return _report(agent, models, runs, live, repeat, max_cost, spent, stop_reason)


def _execute(rt: Runtime, agent: str, case: Case, run: Run) -> Any:
    content = None
    try:
        res = adapter.call(agent, None, case.messages, schema=case.schema, mode="sync",
                           cache_prefix=case.cache_prefix, purpose="eval", runtime=rt)
    except LLMFormatError as exc:
        run.status, run.call_id, run.failures = "format_error", exc.call_id, [redact(str(exc))[:200]]
    except LLMIncompleteError as exc:
        run.status, run.call_id, run.failures = "incomplete", exc.call_id, [f"stop_reason={exc.stop_reason}"]
    except LLMRefusedError as exc:
        run.status, run.call_id, run.failures = "refused", exc.call_id, ["stop_reason=refusal"]
    except BudgetExceeded as exc:
        run.status, run.failures = "error", [f"plafond eval : {exc}"]
    except (LLMCallError, GuardError) as exc:
        run.status, run.failures = "error", [redact(str(exc))[:200]]
    else:
        run.call_id, content = res.call_id, res.content
        run.failures = check(content, case.expected)
        run.status = "failed" if run.failures else "passed"
    row = _row(rt, run.call_id)
    if row is not None:
        run.cost_usd, run.latency_ms = float(row["cost_usd"] or 0), row["latency_ms"]
    return content


# ====================================================================== rapport

def _summary(runs: list[Run]) -> dict[str, Any]:
    executed = [r for r in runs if r.status != "skipped"]
    latencies = [r.latency_ms for r in executed if r.latency_ms is not None]
    passed = sum(r.status == "passed" for r in executed)
    return {
        "runs": len(runs),
        "executed": len(executed),
        "passed": passed,
        "score": round(passed / len(executed), 4) if executed else None,
        "cost_usd": round(sum(r.cost_usd for r in executed), 6),
        "median_latency_ms": statistics.median(latencies) if latencies else None,
        "format_failures": sum(r.status == "format_error" for r in executed),
        "incomplete": sum(r.status == "incomplete" for r in executed),
        "refused": sum(r.status == "refused" for r in executed),
        "errors": sum(r.status == "error" for r in executed),
        "skipped": len(runs) - len(executed),
        "synthetic_runs": sum(r.synthetic for r in runs),
    }


def _report(agent, models, runs, live, repeat, max_cost, spent, stop_reason) -> dict[str, Any]:
    return {
        "agent": agent,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "mode": "live" if live else "dry-run",
        "indicatif": True,
        "cost_basis": "tarif sync",
        "notice": NOTICE,
        "repeat": repeat,
        "max_cost_usd": max_cost,
        "spent_usd": round(spent, 6),
        "stopped": stop_reason,
        "models": {m: _summary([r for r in runs if r.model == m]) for m in models},
        "runs": [dataclasses.asdict(r) for r in runs],
    }


def to_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Évaluation {report['agent']} ({report['mode']}) : INDICATIF", "",
             f"> {report['notice']}", "",
             f"- Généré le {report['generated_at']} ; répétitions : {report['repeat']} ; "
             f"plafond du run : {report['max_cost_usd']:g} $ ; coûts au tarif sync."]
    if report["mode"] == "dry-run":
        lines.append("- Dry-run : réponses simulées, scores et coûts sans valeur de décision.")
    if report["stopped"]:
        lines.append(f"- **{report['stopped']}**")
    lines += ["", "| Modèle | Score | Réussis / exécutés | Coût (tarif sync) | Latence médiane | Échecs de format "
                  "| Incomplets | Refus | Erreurs | Ignorés | Exécutions synthétiques |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for model, s in report["models"].items():
        score = "n/a" if s["score"] is None else f"{s['score'] * 100:.0f} %"
        lat = "n/a" if s["median_latency_ms"] is None else f"{s['median_latency_ms']:.0f} ms"
        lines.append(f"| {model} | {score} | {s['passed']} / {s['executed']} | {s['cost_usd']:.4f} $ | {lat} | "
                     f"{s['format_failures']} | {s['incomplete']} | {s['refused']} | {s['errors']} | "
                     f"{s['skipped']} | {s['synthetic_runs']} |")
    lines += ["", "## Échecs", ""]
    failed = [r for r in report["runs"] if r["status"] not in ("passed", "skipped")]
    if not failed:
        lines.append("Aucun.")
    for r in failed:
        tag = " (synthétique)" if r["synthetic"] else ""
        lines.append(f"- `{r['model']}` / `{r['case_id']}`{tag} #{r['repeat']} : {r['status']} : "
                     + "; ".join(r["failures"]))
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], out_dir: Path | None = None) -> tuple[Path, Path]:
    out_dir = Path(out_dir or REPORTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at"].replace(":", "").replace("-", "").replace("+0000", "Z")
    base = out_dir / f"eval-{report['agent']}-{report['mode']}-{stamp}"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")
    return md_path, json_path


# ====================================================================== CLI

def print_output(case: Case, run: Run, content: Any, out=None) -> None:
    """--show-outputs : console uniquement, et seulement pour un cas synthétique."""
    out = out or sys.stdout
    head = f"--- {run.model} / {case.id} #{run.repeat} ({run.status})"
    if not case.synthetic:
        print(f"{head} : affichage refusé (cas non synthétique) ---", file=out)
        return
    body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
    print(f"{head} ---\n{body}", file=out)


def main(argv: list[str] | None = None, runtime: Runtime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m llm.evalrunner", description=__doc__.splitlines()[0])
    parser.add_argument("--agent", required=True)
    parser.add_argument("--models", required=True, help="liste séparée par des virgules")
    parser.add_argument("--live", action="store_true", help="appels réels (exige aussi LLM_LIVE=1)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST, help="plafond du run en $ (live)")
    parser.add_argument("--cases-dir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--show-outputs", action="store_true",
                        help="affiche la sortie des cas synthétiques (console uniquement)")
    args = parser.parse_args(argv)

    rt = runtime or Runtime.from_env()
    if args.live and not (os.environ.get("LLM_LIVE") == "1" and rt.live):
        print("--live exige LLM_LIVE=1 : rien n'a été lancé.", file=sys.stderr)
        return 2
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    try:
        report = run_eval(args.agent, models, runtime=rt, live=args.live, repeat=args.repeat,
                          max_cost=args.max_cost, cases_dir=args.cases_dir,
                          on_output=print_output if args.show_outputs else None)
    except (EvalError, ConfigError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    md_path, json_path = write_report(report, args.out)
    for model, s in report["models"].items():
        score = "n/a" if s["score"] is None else f"{s['score'] * 100:.0f} %"
        print(f"{model} : score {score}, coût {s['cost_usd']:.4f} $, échecs de format {s['format_failures']}")
    print(f"rapport : {md_path}\n          {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
