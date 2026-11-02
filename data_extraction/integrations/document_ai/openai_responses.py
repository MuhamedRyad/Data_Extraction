from __future__ import annotations

import base64
import io
import json
import os
from dataclasses import dataclass
from typing import Any

from PIL import Image

from ...analyze_layout.contracts import LayoutOrderPrediction, LayoutPageRequest
from ...shared.models import ProviderTextResult, TableCells
from ...template_extraction.errors import TemplateExtractionError


@dataclass(frozen=True, slots=True)
class OpenAIResponsesDocumentAIOptions:
    model: str = "gpt-6.1-sol"
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 90.0
    image_detail: str = "high"
    max_visual_dimension: int = 1800
    max_output_tokens: int = 5000
    store_response: bool = False


class OpenAIResponsesDocumentAIProvider:
    """Selective OpenAI Responses adapter for unresolved image/layout regions.

    The extraction core decides *when* this provider is needed. The provider is
    never authoritative: its output is still routed through normal validation
    and review policy.
    """

    def __init__(
        self,
        api_key: str | None = None,
        options: OpenAIResponsesDocumentAIOptions | None = None,
        http_client: Any | None = None,
    ) -> None:
        self._api_key: str | None = api_key or os.environ.get("OPENAI_API_KEY")
        self._options: OpenAIResponsesDocumentAIOptions = options or OpenAIResponsesDocumentAIOptions()
        self._http_client: Any | None = http_client

    def analyze(self, image_bytes: bytes, mime_type: str, role: str) -> ProviderTextResult:
        if not self._api_key:
            raise TemplateExtractionError(
                "DOCUMENT_AI_CREDENTIALS_MISSING",
                "OPENAI_API_KEY or an explicit api_key is required for automatic AI fallback",
            )
        normalized_bytes: bytes = self._normalize_image(image_bytes)
        data_url: str = self._to_data_url(normalized_bytes, "image/jpeg")
        payload: dict[str, Any] = {
            "model": self._options.model,
            "input": [
                {
                    "role": "system",
                    "content": [{
                        "type": "input_text",
                        "text": (
                            "You are a document extraction provider. Extract only content visibly present in the image. "
                            "Do not infer missing business facts. Preserve visible numbers exactly. If the image contains "
                            "a table, return its visible cell structure. Confidence is your extraction confidence from 0 to 1."
                        ),
                    }],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": f"Region role: {role}. Return faithful extraction only."},
                        {"type": "input_image", "image_url": data_url, "detail": self._options.image_detail},
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "document_region_extraction",
                    "strict": True,
                    "schema": self._extraction_schema(),
                }
            },
            "max_output_tokens": self._options.max_output_tokens,
            "store": self._options.store_response,
        }
        response: dict[str, Any] = self._post(payload)
        body: dict[str, Any] = self._parse_json_output(response)
        text: str = str(body.get("text") or "")
        confidence_raw: Any = body.get("confidence")
        confidence: float | None = float(confidence_raw) if isinstance(confidence_raw, (int, float)) else None
        table_raw: Any = body.get("table")
        table: TableCells | None = None
        if isinstance(table_raw, list):
            rows: list[list[str]] = []
            for row in table_raw:
                if not isinstance(row, list):
                    continue
                rows.append([str(cell) for cell in row])
            table = rows or None
        return ProviderTextResult(
            text=text,
            confidence=confidence,
            structured_table=table,
            provider_name="openai_responses_document_ai",
            model_id=self._options.model,
        )

    def predict_order(self, request: LayoutPageRequest) -> LayoutOrderPrediction:
        if not self._api_key:
            raise TemplateExtractionError(
                "DOCUMENT_AI_CREDENTIALS_MISSING",
                "OPENAI_API_KEY or an explicit api_key is required for automatic AI layout fallback",
            )
        normalized_bytes: bytes = self._normalize_image(request.image_png)
        region_payload: list[dict[str, Any]] = [
            {
                "region_id": region.region_id,
                "kind": region.kind.value,
                "bbox": list(region.bbox) if region.bbox is not None else None,
            }
            for region in request.regions
        ]
        payload: dict[str, Any] = {
            "model": self._options.model,
            "input": [
                {
                    "role": "system",
                    "content": [{
                        "type": "input_text",
                        "text": (
                            "You resolve reading order for already-detected document regions. "
                            "You must return every supplied region id exactly once and may not invent or delete regions."
                        ),
                    }],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": json.dumps({
                            "page_number": request.page_number,
                            "page_width": request.page_width,
                            "page_height": request.page_height,
                            "regions": region_payload,
                        }, ensure_ascii=False)},
                        {"type": "input_image", "image_url": self._to_data_url(normalized_bytes, "image/jpeg"),
                         "detail": self._options.image_detail},
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "document_reading_order",
                    "strict": True,
                    "schema": self._layout_schema(request),
                }
            },
            "max_output_tokens": min(self._options.max_output_tokens, 3000),
            "store": self._options.store_response,
        }
        response: dict[str, Any] = self._post(payload)
        body: dict[str, Any] = self._parse_json_output(response)
        raw_ids: Any = body.get("ordered_region_ids")
        ordered_ids: tuple[str, ...] = tuple(str(item) for item in raw_ids) if isinstance(raw_ids, list) else ()
        confidence_raw: Any = body.get("confidence")
        confidence: float | None = float(confidence_raw) if isinstance(confidence_raw, (int, float)) else None
        return LayoutOrderPrediction(
            page_number=request.page_number,
            ordered_region_ids=ordered_ids,
            confidence=confidence,
            provider_name="openai_responses_document_ai",
            model_id=self._options.model,
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        client: Any = self._http_client
        close_client: bool = False
        if client is None:
            try:
                import httpx
            except ImportError as exc:
                raise TemplateExtractionError(
                    "DOCUMENT_AI_HTTP_DEPENDENCY_MISSING",
                    "Install the 'openai' optional dependency to use automatic AI fallback",
                ) from exc
            client = httpx.Client(timeout=self._options.timeout_seconds)
            close_client = True
        try:
            response: Any = client.post(
                f"{self._options.base_url.rstrip('/')}/responses",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            result: Any = response.json()
        except TemplateExtractionError:
            raise
        except Exception as exc:
            raise TemplateExtractionError(
                "DOCUMENT_AI_REQUEST_FAILED", "OpenAI Responses automatic AI request failed"
            ) from exc
        finally:
            if close_client:
                client.close()
        if not isinstance(result, dict):
            raise TemplateExtractionError("DOCUMENT_AI_INVALID_RESPONSE", "AI response must be a JSON object")
        status: Any = result.get("status")
        if status not in (None, "completed"):
            raise TemplateExtractionError("DOCUMENT_AI_INCOMPLETE_RESPONSE", f"AI response status was {status!r}")
        return result

    @staticmethod
    def _parse_json_output(payload: dict[str, Any]) -> dict[str, Any]:
        direct: Any = payload.get("output_text")
        text: str | None = direct if isinstance(direct, str) and direct.strip() else None
        if text is None:
            chunks: list[str] = []
            output: Any = payload.get("output", [])
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    content: Any = item.get("content", [])
                    if not isinstance(content, list):
                        continue
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "refusal":
                            raise TemplateExtractionError("DOCUMENT_AI_REFUSED", "AI provider refused document extraction")
                        if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                            chunks.append(part["text"])
            text = "".join(chunks).strip() or None
        if text is None:
            raise TemplateExtractionError("DOCUMENT_AI_EMPTY_RESPONSE", "AI provider returned no structured output")
        try:
            decoded: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TemplateExtractionError("DOCUMENT_AI_INVALID_JSON", "AI provider returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise TemplateExtractionError("DOCUMENT_AI_INVALID_PAYLOAD", "AI structured output must be an object")
        return decoded

    def _normalize_image(self, image_bytes: bytes) -> bytes:
        try:
            with Image.open(io.BytesIO(image_bytes)) as source:
                image: Image.Image = source.convert("RGB")
                limit: int = self._options.max_visual_dimension
                if max(image.size) > limit:
                    image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
                stream: io.BytesIO = io.BytesIO()
                image.save(stream, format="JPEG", quality=88, optimize=True)
                return stream.getvalue()
        except Exception as exc:
            raise TemplateExtractionError("DOCUMENT_AI_IMAGE_INVALID", "Could not prepare image for AI fallback") from exc

    @staticmethod
    def _to_data_url(content: bytes, mime_type: str) -> str:
        return f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"

    @staticmethod
    def _extraction_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string"},
                "confidence": {"anyOf": [{"type": "number", "minimum": 0, "maximum": 1}, {"type": "null"}]},
                "table": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                        {"type": "null"},
                    ]
                },
            },
            "required": ["text", "confidence", "table"],
        }

    @staticmethod
    def _layout_schema(request: LayoutPageRequest) -> dict[str, Any]:
        region_ids: list[str] = [region.region_id for region in request.regions]
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "page_number": {"type": "integer", "const": request.page_number},
                "ordered_region_ids": {
                    "type": "array",
                    "items": {"type": "string", "enum": region_ids},
                    "minItems": len(region_ids),
                    "maxItems": len(region_ids),
                },
                "confidence": {"anyOf": [{"type": "number", "minimum": 0, "maximum": 1}, {"type": "null"}]},
            },
            "required": ["page_number", "ordered_region_ids", "confidence"],
        }
