"""Étape 3 : masquage des secrets (règle 7) et alertes Telegram."""
import logging
import urllib.parse

from alerts.telegram import MemoryNotifier, TelegramNotifier
from journal.redact import RedactingFilter, redact
from tests.fakes import fake_anthropic_key, fake_bearer, fake_telegram_token

FAKE_ANTHROPIC_KEY = fake_anthropic_key()
FAKE_TG_TOKEN = fake_telegram_token()


def test_redact_keys_and_emails():
    text = f"key={FAKE_ANTHROPIC_KEY} mail=client.dupont@example.fr tg={FAKE_TG_TOKEN} {fake_bearer()}"
    out = redact(text)
    assert FAKE_ANTHROPIC_KEY not in out
    assert "client.dupont@example.fr" not in out
    assert FAKE_TG_TOKEN not in out
    assert "abc.def-123" not in out
    assert "[EMAIL]" in out and "[SECRET]" in out


def test_redact_env_secret_values(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "valeur-secrete-xyz")
    assert "valeur-secrete-xyz" not in redact("erreur avec valeur-secrete-xyz dedans")


def test_logging_filter_redacts(caplog):
    logger = logging.getLogger("test.redact")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="test.redact"):
        logger.info("appel avec %s pour %s", FAKE_ANTHROPIC_KEY, "x@y.com")
    assert FAKE_ANTHROPIC_KEY not in caplog.text
    assert "x@y.com" not in caplog.text


def test_without_token_only_logs(caplog):
    calls = []
    n = TelegramNotifier(token=None, chat_id=None, opener=lambda *a, **k: calls.append(a))
    with caplog.at_level(logging.WARNING):
        n.send("plafond atteint")
    assert calls == []
    assert "plafond atteint" in caplog.text


def test_with_token_posts_redacted_text():
    sent = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok":true}'

    def opener(req, timeout):
        sent.append(req)
        return Resp()

    n = TelegramNotifier(token=FAKE_TG_TOKEN, chat_id="42", opener=opener)
    n.send("client a@b.fr bloqué")
    assert len(sent) == 1
    req = sent[0]
    assert req.full_url == f"https://api.telegram.org/bot{FAKE_TG_TOKEN}/sendMessage"
    body = urllib.parse.parse_qs(req.data.decode())
    assert body["chat_id"] == ["42"]
    assert "a@b.fr" not in body["text"][0]


def test_network_error_does_not_raise_nor_leak_token(caplog):
    def opener(req, timeout):
        raise OSError(f"connexion impossible à {req.full_url}")

    n = TelegramNotifier(token=FAKE_TG_TOKEN, chat_id="42", opener=opener)
    with caplog.at_level(logging.WARNING):
        n.send("test")  # ne lève pas : une alerte ratée ne doit pas casser l'appel
    assert FAKE_TG_TOKEN not in caplog.text
    assert "échec" in caplog.text


def test_memory_notifier():
    n = MemoryNotifier()
    n.send(f"clé {FAKE_ANTHROPIC_KEY}")
    assert len(n.messages) == 1 and FAKE_ANTHROPIC_KEY not in n.messages[0]
