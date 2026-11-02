from __future__ import annotations

from collections.abc import Mapping

from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..shared.errors import Error, ErrorType
from ..shared.result import Result
from .application import TemplateApplicationService
from .contracts import DocumentTemplate
from .request import (
    TemplateApprovalInput,
    TemplateBuildInput,
    TemplateExtractionInput,
    TemplateMatchInput,
)
from .response import TemplateBuildResult, TemplateExtractionResult, TemplateMatchResult


RequestT = TypeVar("RequestT", bound=BaseModel)


class TemplateExtractionRouter:
    def __init__(self, service: TemplateApplicationService) -> None:
        self._service: TemplateApplicationService = service

    def build_candidate(self, raw_input: Mapping[str, object]) -> Result[TemplateBuildResult]:
        request_result: Result[TemplateBuildInput] = self._validate(
            TemplateBuildInput,
            raw_input,
            "INVALID_TEMPLATE_BUILD_REQUEST",
        )
        if request_result.is_failure:
            return Result.Failure(request_result.unwrap_error())
        return self._service.build_candidate(request_result.unwrap().to_domain())

    def approve(self, raw_input: Mapping[str, object]) -> Result[DocumentTemplate]:
        request_result: Result[TemplateApprovalInput] = self._validate(
            TemplateApprovalInput,
            raw_input,
            "INVALID_TEMPLATE_APPROVAL_REQUEST",
        )
        if request_result.is_failure:
            return Result.Failure(request_result.unwrap_error())
        return self._service.approve(request_result.unwrap().to_domain())

    def match(self, raw_input: Mapping[str, object]) -> Result[TemplateMatchResult]:
        request_result: Result[TemplateMatchInput] = self._validate(
            TemplateMatchInput,
            raw_input,
            "INVALID_TEMPLATE_MATCH_REQUEST",
        )
        if request_result.is_failure:
            return Result.Failure(request_result.unwrap_error())
        return self._service.match(request_result.unwrap().to_domain())

    def extract(self, raw_input: Mapping[str, object]) -> Result[TemplateExtractionResult]:
        request_result: Result[TemplateExtractionInput] = self._validate(
            TemplateExtractionInput,
            raw_input,
            "INVALID_TEMPLATE_EXTRACTION_REQUEST",
        )
        if request_result.is_failure:
            return Result.Failure(request_result.unwrap_error())
        return self._service.extract(request_result.unwrap().to_domain())

    @staticmethod
    def _validate(
        model_type: type[RequestT],
        raw_input: Mapping[str, object],
        code: str,
    ) -> Result[RequestT]:
        try:
            request: RequestT = model_type.model_validate(raw_input)
            return Result.Success(request)
        except ValidationError as exception:
            fields: tuple[str, ...] = tuple(
                ".".join(str(part) for part in item["loc"])
                for item in exception.errors()
            )
            return Result.Failure(
                Error(
                    ErrorType.VALIDATION,
                    code,
                    "The template request is invalid",
                    {"fields": fields},
                )
            )
