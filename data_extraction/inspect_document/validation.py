from __future__ import annotations

import io
import zipfile

from ..shared.errors import ExtractionError
from .options import InspectDocumentOptions
from .request import InspectDocumentRequest


def validate_options(options: InspectDocumentOptions) -> None:
    """Validate options required by inspect and native file discovery only."""
    invalid = (
        options.max_input_bytes < 1
        or options.max_uncompressed_zip_bytes < 1
        or options.max_pages < 1
        or options.max_regions_per_page < 1
        or options.max_sheet_cells < 1
        or options.max_image_pixels < 1
        or options.max_image_regions < 0
        or not 72 <= options.image_dpi <= 600
        or not 36 <= options.min_image_dpi <= options.image_dpi
    )
    if invalid:
        raise ExtractionError("INVALID_OPTIONS", "Invalid inspect document options")


def validate_upload(request: InspectDocumentRequest, options: InspectDocumentOptions) -> None:
    """Validate the uploaded document and upload-related safety limits only."""
    if len(request.content) == 0:
        raise ExtractionError("EMPTY_FILE", "File is empty")
    if len(request.content) > options.max_input_bytes:
        raise ExtractionError("FILE_TOO_LARGE", "Input exceeds file size limit")

    if zipfile.is_zipfile(io.BytesIO(request.content)):
        with zipfile.ZipFile(io.BytesIO(request.content)) as archive:
            sizes: int = sum(entry.file_size for entry in archive.infolist())
            if sizes > options.max_uncompressed_zip_bytes or len(archive.infolist()) > 3000:
                raise ExtractionError(
                    "ZIP_LIMIT_EXCEEDED",
                    "Expanded archive exceeds safety limits",
                )
            if any(entry.flag_bits & 1 for entry in archive.infolist()):
                raise ExtractionError(
                    "ENCRYPTED_ARCHIVE",
                    "Encrypted archives are not supported",
                )


__all__ = ["validate_options", "validate_upload"]
