"""Backward-compatible import path; implementation lives in data_extraction.inspect_document.service."""
from data_extraction.inspect_document.service import InspectDocumentService

InspectFileAction = InspectDocumentService

__all__ = ["InspectFileAction"]
