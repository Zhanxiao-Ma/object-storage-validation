"""Run-scoped redaction for credentials returned by real SDK operations."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import threading

_active = ContextVar("osv_secret_registry", default=None)
_FIELDS = {"accesskey", "accesskeyid", "secretkey", "secretaccesskey", "sessiontoken", "password", "token"}


class SecretRegistry:
    """Keep actual credentials usable in memory while scrubbing later diagnostics.

    Register environment and SDK output values as they arrive; use a locked shared
    registry so forked worker threads redact dynamically issued credentials too.
    """
    def __init__(self):
        self._values = set()
        self._lock = threading.RLock()

    def add(self, value):
        if isinstance(value, str) and len(value) >= 4:
            variants = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
            with self._lock:
                self._values.update(variants)

    def environment(self, environment):
        for name, value in environment.items():
            if any(word in name.upper() for word in ("SECRET", "TOKEN", "PASSWORD", "ACCESS_KEY")):
                self.add(value)

    def payload(self, value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).replace("_", "").lower() in _FIELDS:
                    self.add(child)
                self.payload(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                self.payload(child)

    def redact(self, text):
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            text = text.replace(value, "[REDACTED]")
        return text


def current_registry():
    return _active.get()


@contextmanager
def secret_scope():
    """Bind one shared registry to a run, restoring its caller on every exit."""
    registry = SecretRegistry()
    token = _active.set(registry)
    try:
        yield registry
    finally:
        _active.reset(token)
