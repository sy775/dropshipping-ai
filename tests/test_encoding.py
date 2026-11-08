"""Spec 003, point 8 : fichiers texte lus et écrits en UTF-8 explicite, y compris sous Windows."""
import ast
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("alerts", "journal", "llm", "tools", "tests")


def test_mutations_catalogue_keeps_accented_labels():
    entries = yaml.safe_load((REPO / "tools" / "mutations.yaml").read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in entries}
    assert by_id["m002"]["description"] == "plafond par boutique supprimé"
    assert by_id["m004"]["description"] == "sync : contrôle avec montant 0"


def _text_io_without_encoding(path: Path, root: Path = REPO) -> list[str]:
    """Appels open(), read_text(), write_text() en mode texte sans argument encoding=."""
    bad = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        if name not in ("open", "read_text", "write_text"):
            continue
        if any(k.arg == "encoding" for k in node.keywords):
            continue
        mode = node.args[1] if name == "open" and len(node.args) > 1 else next(
            (k.value for k in node.keywords if k.arg == "mode"), None)
        if isinstance(mode, ast.Constant) and "b" in str(mode.value):
            continue                                                       # mode binaire
        bad.append(f"{path.relative_to(root).as_posix()}:{node.lineno} {name}()")
    return bad


def test_all_text_io_declares_utf8():
    bad = [b for d in SOURCE_DIRS for p in sorted((REPO / d).rglob("*.py")) for b in _text_io_without_encoding(p)]
    assert bad == []


def test_detector_catches_a_missing_encoding(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text("from pathlib import Path\nPath('x').read_text()\nopen('y', 'rb')\n", encoding="utf-8")
    assert _text_io_without_encoding(sample, tmp_path) == ["sample.py:2 read_text()"]
