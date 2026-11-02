"""Backward-compatible import path; implementation lives in data_extraction.shared.models."""
from data_extraction.shared.models import *  # noqa: F401,F403
from data_extraction.inspect_document.manifest import FileManifest, PageManifest, RegionManifest
from data_extraction.inspect_document.models import CandidateRegion, InspectedDocument, PageCandidates
