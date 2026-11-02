from __future__ import annotations

from typing import Protocol

from .contracts import DocumentTemplate
from .errors import TemplateExtractionError


class TemplateStore(Protocol):
    def save(self, template: DocumentTemplate) -> None: ...
    def get(self, template_id: str, version: int) -> DocumentTemplate | None: ...
    def list_approved(self, family: str | None = None) -> tuple[DocumentTemplate, ...]: ...
    def next_version(self, template_id: str) -> int: ...


class InMemoryTemplateStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, int], DocumentTemplate] = {}

    def save(self, template: DocumentTemplate) -> None:
        key = (template.template_id, template.version)
        existing = self._items.get(key)
        if existing is not None and existing != template:
            raise TemplateExtractionError(
                "TEMPLATE_VERSION_CONFLICT", "Template versions are immutable; create a new version"
            )
        self._items[key] = template

    def get(self, template_id: str, version: int) -> DocumentTemplate | None:
        return self._items.get((template_id, version))

    def list_approved(self, family: str | None = None) -> tuple[DocumentTemplate, ...]:
        items = [
            item for item in self._items.values()
            if item.status.value == "approved" and (family is None or item.family == family)
        ]
        items.sort(key=lambda item: (item.template_id, -item.version))
        return tuple(items)

    def next_version(self, template_id: str) -> int:
        versions = [version for current_id, version in self._items if current_id == template_id]
        return max(versions, default=0) + 1
