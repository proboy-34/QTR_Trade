import hashlib
import hmac
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fastapi import Depends, Header

from app.core.config import Settings, get_settings
from app.core.errors import QTRError


class Role(StrEnum):
    VIEWER = "viewer"
    RESEARCHER = "researcher"
    TRADER = "trader"
    ADMIN = "admin"
    # Retained V1 roles. OPERATOR is equivalent to ADMIN; AUDITOR to VIEWER.
    OPERATOR = "operator"
    AUDITOR = "auditor"


# A role implies the roles listed for it. Viewer access is implied by every role.
IMPLIED: dict[Role, frozenset[Role]] = {
    Role.ADMIN: frozenset(Role),
    Role.OPERATOR: frozenset(Role),
    Role.TRADER: frozenset({Role.TRADER, Role.VIEWER, Role.AUDITOR}),
    Role.RESEARCHER: frozenset({Role.RESEARCHER, Role.VIEWER, Role.AUDITOR}),
    Role.VIEWER: frozenset({Role.VIEWER, Role.AUDITOR}),
    Role.AUDITOR: frozenset({Role.VIEWER, Role.AUDITOR}),
}


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[Role]
    provider: str

    def can(self, role: Role) -> bool:
        return any(role in IMPLIED[held] for held in self.roles)


class AuthenticationError(QTRError):
    code = "UNAUTHENTICATED"
    status_code = 401


class AuthorizationError(QTRError):
    code = "FORBIDDEN"
    status_code = 403


class AuthProvider(Protocol):
    async def authenticate(self, credential: str | None) -> Principal: ...


class LocalDemoAuthProvider:
    """Password-free local identity. Never use for an internet-exposed deployment."""

    async def authenticate(self, credential: str | None = None) -> Principal:
        return Principal("local-operator", frozenset(Role), "local_demo")


class StaticTokenAuthProvider:
    """Bearer tokens configured server-side as ``token:role|role``. Tokens are compared by digest."""

    def __init__(self, configuration: str) -> None:
        self._tokens: dict[str, frozenset[Role]] = {}
        for entry in configuration.split(","):
            if ":" not in entry:
                continue
            token, roles = entry.strip().split(":", 1)
            digest = hashlib.sha256(token.encode()).hexdigest()
            self._tokens[digest] = frozenset(Role(item.strip()) for item in roles.split("|") if item.strip())

    async def authenticate(self, credential: str | None) -> Principal:
        if not credential or not credential.lower().startswith("bearer "):
            raise AuthenticationError("Bearer token required")
        digest = hashlib.sha256(credential[7:].strip().encode()).hexdigest()
        for known, roles in self._tokens.items():
            if hmac.compare_digest(known, digest):
                return Principal(f"token:{known[:8]}", roles, "static_token")
        raise AuthenticationError("Invalid token")


def auth_provider(settings: Settings) -> AuthProvider:
    if settings.auth_mode == "token":
        return StaticTokenAuthProvider(settings.auth_tokens)
    return LocalDemoAuthProvider()


async def local_principal(authorization: str | None = Header(None)) -> Principal:
    return await auth_provider(get_settings()).authenticate(authorization)


def require(role: Role):
    async def dependency(principal: Principal = Depends(local_principal)) -> Principal:
        if not principal.can(role):
            raise AuthorizationError(f"Role '{role.value}' is required")
        return principal

    return dependency
