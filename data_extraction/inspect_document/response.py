from __future__ import annotations

from ..shared.contracts import StrictContract
from .manifest import FileManifest


class InspectDocumentResponse(StrictContract):
    manifest: FileManifest


__all__ = [ "InspectDocumentResponse"]
