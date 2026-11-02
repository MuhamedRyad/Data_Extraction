from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StructuredDocumentAnalysisOptions(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    minimum_kind_confidence: float = 0.72
    minimum_suspicion_confidence: float = 0.40
