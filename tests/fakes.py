"""Faux secrets construits à l'exécution, pour tester le masquage.

Aucun littéral du dépôt ne ressemble à une clé d'API : les scanners de secrets et la relecture
ne doivent rien trouver, et ces valeurs ne correspondent à aucun compte réel.
"""

ANTHROPIC_KEY_PREFIX = "-".join(["sk", "ant"])


def fake_anthropic_key() -> str:
    return "-".join([ANTHROPIC_KEY_PREFIX, "test" + "0" * 2, "Xy" * 24])


def fake_telegram_token() -> str:
    bot_id = "".join(str(d) for d in range(1, 10))          # 9 chiffres
    return bot_id + ":" + "Tz" * 17                         # 34 caractères après les deux-points
