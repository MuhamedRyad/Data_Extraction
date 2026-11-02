"""Stable validation interface for the feature."""
from __future__ import annotations

from .inspect_document.validation import validate_options, validate_upload

__all__ = ["validate_options", "validate_upload"]
