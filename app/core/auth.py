from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fastapi import Header


class Role(StrEnum):
    OPERATOR = "operator"
    RESEARCHER = "researcher"
    AUDITOR = "auditor"


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[Role]
    provider: str

    def can(self, role: Role) -> bool:
        return role in self.roles or Role.OPERATOR in self.roles


class AuthProvider(Protocol):
    async def authenticate(self, credential: str | None) -> Principal: ...


class LocalDemoAuthProvider:
    """Password-free local identity. Never use for an internet-exposed deployment."""

    async def authenticate(self, credential: str | None = None) -> Principal:
        return Principal("local-operator", frozenset(Role), "local_demo")


async def local_principal(authorization: str | None = Header(None)) -> Principal:
    return await LocalDemoAuthProvider().authenticate(authorization)

