from __future__ import annotations

from ..shared.models import CandidateRegion, ExtractedRegion, ExtractionMethod, RegionKind


class ExtractTextAction:
    def execute(self, region: CandidateRegion) -> ExtractedRegion:
        if region.kind != RegionKind.TEXT:
            raise ValueError("ExtractTextAction accepts text candidates only")
        text: str = region.text or ""
        return ExtractedRegion(region.region_id, RegionKind.TEXT, region.bbox,
                               ExtractionMethod.NATIVE, text, None, None,
                               not text.strip(), ("empty_text",) if not text.strip() else (), "native_text_direct", (ExtractionMethod.NATIVE,), RegionKind.TEXT)
