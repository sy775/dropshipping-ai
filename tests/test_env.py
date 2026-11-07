"""Correction 3 : chargement de .env par load_env(), sans écraser l'environnement existant."""
from llm.runtime import load_env

import os


def test_existing_env_var_wins_over_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DROPSHIP_TEST_A=depuis_fichier\nDROPSHIP_TEST_B=depuis_fichier\n", encoding="utf-8")
    monkeypatch.setenv("DROPSHIP_TEST_A", "depuis_environnement")
    # B n'existe pas avant : on l'enregistre auprès de monkeypatch pour qu'il soit retiré après le test.
    monkeypatch.setenv("DROPSHIP_TEST_B", "x")
    monkeypatch.delenv("DROPSHIP_TEST_B")

    assert load_env(env_file) is True
    assert os.environ["DROPSHIP_TEST_A"] == "depuis_environnement"   # override=False
    assert os.environ["DROPSHIP_TEST_B"] == "depuis_fichier"          # variable absente : chargée


def test_missing_dotenv_is_not_an_error(tmp_path):
    assert load_env(tmp_path / "absent.env") is False
