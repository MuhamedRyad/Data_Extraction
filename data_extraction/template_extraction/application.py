from __future__ import annotations

from ..shared.errors import ExtractionError, error_from_exception
from ..shared.result import Result
from .contracts import DocumentTemplate
from .request import (
    TemplateApprovalRequest,
    TemplateBuildRequest,
    TemplateExtractionRequest,
    TemplateMatchRequest,
)
from .response import TemplateBuildResult, TemplateExtractionResult, TemplateMatchResult
from .workflow import TemplateWorkflowService


class TemplateApplicationService:
    """Result-based boundary over the exception-compatible template workflow."""

    def __init__(self, workflow: TemplateWorkflowService) -> None:
        self._workflow = workflow

    def build_candidate(self, request: TemplateBuildRequest) -> Result[TemplateBuildResult]:
        try:
            return Result.Success(self._workflow.build_template_candidate(request))
        except ExtractionError as exception:
            return Result.Failure(error_from_exception(exception))

    def approve(self, request: TemplateApprovalRequest) -> Result[DocumentTemplate]:
        try:
            return Result.Success(self._workflow.approve_template(request))
        except ExtractionError as exception:
            return Result.Failure(error_from_exception(exception))

    def match(self, request: TemplateMatchRequest) -> Result[TemplateMatchResult]:
        try:
            return Result.Success(self._workflow.match_template(request))
        except ExtractionError as exception:
            return Result.Failure(error_from_exception(exception))

    def extract(self, request: TemplateExtractionRequest) -> Result[TemplateExtractionResult]:
        try:
            return Result.Success(self._workflow.extract_with_template(request))
        except ExtractionError as exception:
            return Result.Failure(error_from_exception(exception))
