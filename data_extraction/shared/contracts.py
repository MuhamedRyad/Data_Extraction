from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import DocumentInput


class StrictContract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, arbitrary_types_allowed=True)


class DocumentRequestContract(StrictContract):
    file_name: str = Field(min_length=1, max_length=512)
    content: bytes = Field(min_length=1)
    source_id: str = Field(min_length=1, max_length=512)

    @field_validator("file_name", "source_id")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value cannot be blank")
        return value

    @classmethod
    def from_domain(cls, document: DocumentInput) -> Self:
        return cls(file_name=document.file_name, content=document.content, source_id=document.source_id)

    def to_domain(self) -> DocumentInput:
        return DocumentInput(
            file_name=self.file_name,
            content=self.content,
            source_id=self.source_id,
        )
