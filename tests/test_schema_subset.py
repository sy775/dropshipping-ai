"""Correctif 4 : schéma de sortie contrôlé avant tout appel (sous-ensemble des sorties structurées)."""
import pytest
import yaml

from llm import adapter
from llm.guards import SchemaNotSupported, check_schema
from pathlib import Path

from tests.conftest import rows

REPO_ROOT = Path(__file__).resolve().parents[1]

MSG = [{"role": "user", "content": "Où est ma commande ?"}]


def obj(props, required=None, **extra):
    return {"type": "object", "properties": props, "required": list(props if required is None else required),
            "additionalProperties": False, **extra}


CONFORME = obj({
    "reponse": {"type": "string", "description": "texte au client"},
    "categorie": {"type": "string", "enum": ["livraison", "retour", "autre"]},
    "escalade": {"type": "boolean"},
    "suivi": {"anyOf": [{"type": "string", "format": "uri"}, {"type": "null"}]},
    "lignes": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/ligne"}},
    "reference": {"type": "string", "pattern": r"^CMD-\d{4,8}$"},
}, **{"$defs": {"ligne": obj({"sku": {"type": "string"}, "qte": {"type": "integer"}})}})


# ---------------------------------------------------------------- contrôle pur

def test_conforming_schema_accepted():
    check_schema(CONFORME)
    check_schema(None)


@pytest.mark.parametrize("path", sorted((REPO_ROOT / "evals").glob("*/_schema.yaml")), ids=lambda p: p.parent.name)
def test_eval_schemas_conform(path):
    check_schema(yaml.safe_load(path.read_text(encoding="utf-8")))


def test_nested_object_without_additional_properties_refused():
    schema = obj({"client": {"type": "object", "properties": {"nom": {"type": "string"}}, "required": ["nom"]}})
    with pytest.raises(SchemaNotSupported, match=r"#/properties/client : objet sans additionalProperties: false"):
        check_schema(schema)


def test_object_in_array_items_and_defs_checked():
    with pytest.raises(SchemaNotSupported, match="items : objet sans additionalProperties"):
        check_schema(obj({"l": {"type": "array", "items": {"type": "object", "properties": {}}}}))
    with pytest.raises(SchemaNotSupported, match=r"\$defs/x : objet sans additionalProperties"):
        check_schema(obj({"a": {"$ref": "#/$defs/x"}}, **{"$defs": {"x": {"type": "object"}}}))


@pytest.mark.parametrize("prop, expected", [
    ({"type": "number", "minimum": 0}, "'minimum'"),
    ({"type": "integer", "maximum": 5}, "'maximum'"),
    ({"type": "number", "multipleOf": 2}, "'multipleOf'"),
    ({"type": "string", "minLength": 1}, "'minLength'"),
    ({"type": "string", "maxLength": 80}, "'maxLength'"),
    ({"type": "array", "items": {"type": "string"}, "maxItems": 3}, "'maxItems'"),
    ({"type": "array", "items": {"type": "string"}, "minItems": 2}, "minItems"),
    ({"type": "array", "items": {"type": "string"}, "uniqueItems": True}, "'uniqueItems'"),
    ({"oneOf": [{"type": "string"}, {"type": "null"}]}, "'oneOf'"),
    ({"type": "string", "format": "iban"}, "format non supporté"),
    ({"type": "string", "enum": [{"a": 1}]}, "enum"),
    ({"type": "decimal"}, "type non supporté"),
    ({"type": "object", "properties": {}, "additionalProperties": True}, "additionalProperties: false"),
    ({"$ref": "https://example.org/s.json"}, "externe"),
    ({"type": "string", "pattern": r"(a)\1"}, "référence arrière"),
    ({"type": "string", "pattern": r"^(?!x)"}, "assertion"),
    ({"type": "string", "pattern": r"\bmot\b"}, "limite de mot"),
    ({"type": "string", "pattern": r"^a{1,500}$"}, "trop grand"),
])
def test_unsupported_keywords_refused(prop, expected):
    with pytest.raises(SchemaNotSupported, match=expected):
        check_schema(obj({"champ": prop}))


def test_recursive_schema_refused():
    arbre = obj({"nom": {"type": "string"}, "enfants": {"type": "array", "items": {"$ref": "#/$defs/noeud"}}})
    with pytest.raises(SchemaNotSupported, match="récursif"):
        check_schema(obj({"racine": {"$ref": "#/$defs/noeud"}}, **{"$defs": {"noeud": arbre}}))
    with pytest.raises(SchemaNotSupported, match="récursif"):
        check_schema(obj({"moi": {"anyOf": [{"$ref": "#"}, {"type": "null"}]}}))
    with pytest.raises(SchemaNotSupported, match="introuvable"):
        check_schema(obj({"a": {"$ref": "#/$defs/absent"}}))


def test_complexity_limits():
    optional = obj({f"p{i}": {"type": "string"} for i in range(25)}, required=[])
    with pytest.raises(SchemaNotSupported, match="25 paramètres optionnels"):
        check_schema(optional)
    unions = obj({f"u{i}": {"type": ["string", "null"]} for i in range(17)})
    with pytest.raises(SchemaNotSupported, match="17 paramètres à type union"):
        check_schema(unions)
    check_schema(obj({f"p{i}": {"type": "string"} for i in range(24)}, required=[]))


# ---------------------------------------------------------------- adaptateur : refus sans appel

def test_adapter_accepts_conforming_schema(rt):
    schema = obj({"client": obj({"nom": {"type": "string"}}), "escalade": {"type": "boolean"}})
    res = adapter.call("sav", "shop-a", MSG, schema=schema, runtime=rt)
    assert len(rt.fake.requests) == 1 and rt.fake.requests[0].schema == schema
    assert res.content == {"client": {"nom": ""}, "escalade": False}
    assert rows(rt, call_id=res.call_id)[0]["status"] == "ok"


@pytest.mark.parametrize("mode", ["sync", "batch"])
def test_adapter_refuses_nested_object_without_additional_properties(rt, mode):
    bad = obj({"client": {"type": "object", "properties": {"nom": {"type": "string"}}, "required": ["nom"]}})
    messages = MSG if mode == "sync" else [MSG]
    with pytest.raises(SchemaNotSupported):
        adapter.call("sav", "shop-a", messages, schema=bad, mode=mode, runtime=rt)
    assert rt.fake.requests == [] and rt.fake.batches == {}           # aucun appel au fournisseur
    (row,) = rows(rt)
    assert row["status"] == "refused" and row["cost_usd"] == 0 and row["stop_reason"] is None
    assert "additionalProperties" in row["error"]
