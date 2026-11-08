"""Correctif 6 : la requête de calibrage de docs/procedure-rapprochement-mensuel.md s'exécute sur le vrai schéma."""
import re
from pathlib import Path

import pytest

from llm import adapter
from llm.pricing import Usage
from llm.providers.base import ProviderResponse
from llm.providers.fake import FakeProvider

DOC = Path(__file__).resolve().parents[1] / "docs" / "procedure-rapprochement-mensuel.md"
WEB = {"type": "web_search_20250305", "name": "web_search"}
CONV = [{"role": "user", "content": "Niche bougies"}]


def _sql_blocks() -> list[str]:
    return re.findall(r"```sql\n(.*?)```", DOC.read_text(encoding="utf-8"), flags=re.S)


def test_calibration_query_measures_tokens_per_search(make_runtime):
    # Chaque élément : 2 recherches, 3000 tokens relus par recherche, aucune autre entrée.
    usage = Usage(tokens_in=6000, tokens_out=100, web_search_requests=2)
    rt = make_runtime(provider=FakeProvider(
        responder=lambda req: ProviderResponse(text="ok", usage=usage, stop_reason="end_turn")))
    h = adapter.call("veille", None, [CONV, CONV], tools=[WEB], runtime=rt)
    rt.fake.finish(h.batch_id)
    adapter.collect_batch(h.batch_id, runtime=rt)

    (sql,) = [b for b in _sql_blocks() if "tokens_par_recherche" in b]
    settings = rt.settings
    assert (settings.agents["veille"].web_search_max_uses, settings.search_tokens_estimate,
            1 + settings.input_margin) == (5, 4000, pytest.approx(1.20))      # valeurs recopiées dans la doc
    (row,) = rt.conn.execute(sql.replace("AAAA-MM", "2026-11")).fetchall()
    assert row["elements"] == 2 and row["reel_in"] == 12000 and row["recherches"] == 4
    assert row["ecart_pct"] < 0                                        # l'estimation reste un majorant
    # 3000 par recherche, moins la petite base textuelle estimée (quelques dizaines de tokens) répartie sur 4
    assert 0 < row["base_est"] < 200
    assert row["tokens_par_recherche"] == pytest.approx((12000 - row["base_est"]) / 4, abs=1)
    assert 2950 <= row["tokens_par_recherche"] <= 3000


def test_all_procedure_queries_run(rt):
    for sql in _sql_blocks():
        for statement in filter(str.strip, sql.replace("AAAA-MM", "2026-11").split(";")):
            if statement.strip().startswith("--") and "SELECT" not in statement and "WITH" not in statement:
                continue
            rt.conn.execute(statement).fetchall()
