from __future__ import annotations

from ..shared.errors import ExtractionError


class FieldVerificationError(ExtractionError):
    """Safe error raised by the field verification feature."""
