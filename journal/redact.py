"""Masquage des secrets et adresses email avant toute écriture de log ou d'alerte (règle 7)."""
from __future__ import annotations

import logging
import os
import re

_SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY")

_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),                  # clés Anthropic
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),                # autres clés de type sk-
    re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{30,}"),            # jetons de bot Telegram
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"),         # en-têtes Authorization
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def redact(text: object) -> str:
    out = str(text)
    for var in _SECRET_ENV_VARS:
        value = os.environ.get(var)
        if value and len(value) >= 8:
            out = out.replace(value, "[SECRET]")
    for pattern in _PATTERNS:
        out = pattern.sub("[SECRET]", out)
    return _EMAIL.sub("[EMAIL]", out)


class RedactingFilter(logging.Filter):
    """Filtre de logging : masque le message formaté avant qu'un handler ne l'écrive."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def install_redaction(logger: logging.Logger | None = None) -> None:
    """Ajoute le filtre sur le logger donné (racine par défaut) et sur ses handlers."""
    logger = logger or logging.getLogger()
    for target in (logger, *logger.handlers):
        if not any(isinstance(f, RedactingFilter) for f in target.filters):
            target.addFilter(RedactingFilter())
