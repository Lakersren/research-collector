"""JSON logs with per-job/source context and credential redaction."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from aris.paths import LOGS

_context = ContextVar('corpus_log_context', default={})


@contextmanager
def log_context(**values):
    token = _context.set({**_context.get(), **values})
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets=None):
        super().__init__()
        self.secrets = sorted(set(secrets if secrets is not None else [
            value for key, value in os.environ.items()
            if value and any(word in key.upper() for word in ('PASSWORD', 'SECRET', 'TOKEN', 'ACCESS_KEY'))
        ]), key=len, reverse=True)

    def redact(self, text):
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, '[REDACTED]')
        text = re.sub(r'(https?://)[^/\s@]+@', r'\1[REDACTED]@', text)
        text = re.sub(r'(?i)((?:x-amz-(?:signature|credential|security-token)|password|secret|token)=)[^&\s\"\']+',
                      r'\1[REDACTED]', text)
        return text

    def format(self, record):
        row = {'time': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
               'level': record.levelname, 'logger': record.name, **_context.get(),
               'message': record.getMessage()}
        if record.exc_info:
            row['exception'] = self.formatException(record.exc_info)
        # Redact before JSON escaping, including tracebacks and context fields.
        return json.dumps({key: self.redact(value) if isinstance(value, str) else value
                           for key, value in row.items()}, ensure_ascii=False)


def setup_logging(component='pipeline'):
    LOGS.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('corpus')
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    logger.setLevel(os.getenv('LOG_LEVEL', 'INFO').upper())
    logger.propagate = False
    formatter = JsonFormatter()
    file_handler = RotatingFileHandler(LOGS / f'{component}.log', maxBytes=10*1024*1024,
                                       backupCount=10, encoding='utf-8')
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger
