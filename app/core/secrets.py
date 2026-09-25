import os
from typing import Protocol


class SecretProvider(Protocol):
    def get(self, name: str) -> str | None: ...


class EnvironmentSecretProvider:
    """Local V1 provider; replaceable by a vault without changing consumers."""

    def get(self, name: str) -> str | None:
        return os.getenv(name) or None

    def configured(self, *names: str) -> bool:
        return all(self.get(name) for name in names)


def mask_secret(value: str | None) -> str:
    if not value:
        return "not configured"
    return f"••••{value[-4:]}"

