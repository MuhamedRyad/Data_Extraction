from __future__ import annotations

from dataclasses import dataclass

from ..shared.models import DocumentInput
from .response import FieldDiscoveryKind


@dataclass(frozen=True, slots=True)
class FieldDiscoveryRequest:
    document: DocumentInput
    kinds: tuple[FieldDiscoveryKind, ...] = (
        FieldDiscoveryKind.EMAIL,
        FieldDiscoveryKind.URL,
        FieldDiscoveryKind.PHONE,
        FieldDiscoveryKind.DATE,
        FieldDiscoveryKind.TIME,
        FieldDiscoveryKind.MONEY,
        FieldDiscoveryKind.IDENTIFIER,
        FieldDiscoveryKind.NUMERIC_CODE,
    )

from pydantic import field_validator
from ..shared.contracts import DocumentRequestContract, StrictContract


class FieldDiscoveryInput(StrictContract):
    document: DocumentRequestContract
    kinds: tuple[FieldDiscoveryKind, ...] = (
        FieldDiscoveryKind.EMAIL,
        FieldDiscoveryKind.URL,
        FieldDiscoveryKind.PHONE,
        FieldDiscoveryKind.DATE,
        FieldDiscoveryKind.TIME,
        FieldDiscoveryKind.MONEY,
        FieldDiscoveryKind.IDENTIFIER,
        FieldDiscoveryKind.NUMERIC_CODE,
    )

    @field_validator("kinds", mode="before")
    @classmethod
    def normalize_kinds(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                item if isinstance(item, FieldDiscoveryKind) else FieldDiscoveryKind(item)
                for item in value
            )
        return value

    def to_domain(self) -> FieldDiscoveryRequest:
        return FieldDiscoveryRequest(document=self.document.to_domain(), kinds=self.kinds)
