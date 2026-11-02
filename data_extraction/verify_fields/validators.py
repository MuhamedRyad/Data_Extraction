from __future__ import annotations

import re

from .errors import FieldVerificationError
from .options import FieldVerificationOptions
from .request import FieldVerificationRequest


def validate_request(request: FieldVerificationRequest, options: FieldVerificationOptions) -> None:
    if not request.document.source_id.strip() or not request.document.file_name.strip():
        raise FieldVerificationError(
            "INVALID_FIELD_VERIFICATION_REQUEST", "Document identity is required"
        )
    if not request.fields or len(request.fields) > options.max_fields:
        raise FieldVerificationError(
            "INVALID_FIELD_VERIFICATION_REQUEST", "Field definition count is invalid"
        )
    names: set[str] = set()
    for field in request.fields:
        if not field.name.strip() or len(field.name) > 100 or field.name in names:
            raise FieldVerificationError(
                "INVALID_FIELD_VERIFICATION_REQUEST", "Field names must be unique and bounded"
            )
        names.add(field.name)
        direct_bbox = field.normalized_value_bbox is not None
        if (not field.labels and not direct_bbox) or len(field.labels) > 32 or any(
            not label.strip() or len(label) > 200 for label in field.labels
        ):
            raise FieldVerificationError(
                "INVALID_FIELD_VERIFICATION_REQUEST", "Field labels or direct template bbox are required"
            )
        if field.page_number is not None and field.page_number < 1:
            raise FieldVerificationError(
                "INVALID_FIELD_VERIFICATION_REQUEST", "Field page number is invalid"
            )
        for bbox in (field.normalized_value_bbox, field.normalized_label_bbox):
            if bbox is None:
                continue
            x0, y0, x1, y1 = bbox
            if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
                raise FieldVerificationError(
                    "INVALID_FIELD_VERIFICATION_REQUEST", "Normalized field bbox must be within 0..1"
                )
        if not 0.0 <= field.minimum_confidence <= 1.0:
            raise FieldVerificationError(
                "INVALID_FIELD_VERIFICATION_REQUEST", "Field confidence threshold is invalid"
            )
        if not field.pattern or len(field.pattern) > 512:
            raise FieldVerificationError(
                "INVALID_FIELD_VERIFICATION_REQUEST", "Field schema is invalid"
            )
        try:
            re.compile(field.pattern)
        except re.error as error:
            raise FieldVerificationError(
                "INVALID_FIELD_VERIFICATION_REQUEST", "Field schema is invalid"
            ) from error
        if field.template is not None and (not field.template or len(field.template) > 512):
            raise FieldVerificationError(
                "INVALID_FIELD_VERIFICATION_REQUEST", "Field template is invalid"
            )
    if not 72 <= options.render_dpi <= 600:
        raise FieldVerificationError(
            "INVALID_FIELD_VERIFICATION_OPTIONS", "Render DPI is outside the safe range"
        )
    if options.crop_padding_x < 0 or options.crop_padding_y < 0:
        raise FieldVerificationError(
            "INVALID_FIELD_VERIFICATION_OPTIONS", "Crop padding cannot be negative"
        )
    if not 0.0 <= options.minimum_vertical_overlap <= 1.0:
        raise FieldVerificationError(
            "INVALID_FIELD_VERIFICATION_OPTIONS", "Vertical overlap threshold is invalid"
        )
    if options.consensus_count < 2:
        raise FieldVerificationError(
            "INVALID_FIELD_VERIFICATION_OPTIONS", "Consensus count must be at least two"
        )
    if not 2 <= options.schema_retry_upscale_factor <= 4:
        raise FieldVerificationError(
            "INVALID_FIELD_VERIFICATION_OPTIONS", "Schema retry upscale factor must be between two and four"
        )
    for value in (
        options.max_field_occurrences,
        options.max_candidates_per_field,
        options.max_candidate_chars,
        options.max_page_pixels,
        options.max_crop_pixels,
        options.max_field_ocr_calls,
    ):
        if value < 1:
            raise FieldVerificationError(
                "INVALID_FIELD_VERIFICATION_OPTIONS", "Verification limits must be positive"
            )
