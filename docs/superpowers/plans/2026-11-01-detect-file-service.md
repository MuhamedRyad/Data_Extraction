# Detect File Service Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `DetectFileService` the only detect-file implementation and use `DetectFileRequest` as its only feature input contract.

**Architecture:** Keep request validation in `DetectFileRequest`, keep response/error/result contracts unchanged, and move the existing signature-based detection logic into `DetectFileService`. Update the inspect feature and detect-file exports/tests so no caller imports the removed detect-file action or feature-local `DocumentInput` alias.

**Tech Stack:** Python 3.11, Pydantic v2, pytest project layout.

**Spec:** `docs/superpowers/specs/2026-11-01-detect-file-service-design.md`

## Global Constraints

- Preserve the existing file detection behavior and error codes.
- `DetectFileService.execute` accepts `DetectFileRequest` and returns `Result[DetectFileResponse]`.
- Do not remove the shared `DocumentInput` model because other features still use it.
- Per user request, do not add or run tests as a verification step.

## Review Focus

- Existing detect-file callers must use the new `Result` response path.
- The inspect feature must convert its domain input into `DetectFileRequest` and preserve extraction errors.
- The package export must not expose the removed `DetectFileAction`.
- Signature precedence and existing unsupported-file error codes must remain unchanged.
- The legacy feature test import must not reference deleted symbols.

### Task 1: Consolidate detect-file implementation

**Files:**
- Modify: `data_extraction/detect_file/request.py`
- Modify: `data_extraction/detect_file/service.py`
- Modify: `data_extraction/detect_file/__init__.py`

**Interfaces:**
- Consumes: `DetectFileRequest`, `DetectFileOptions`, `DetectFileResponse`, `Result`, and shared `ExtractionError`/`FileKind`.
- Produces: `DetectFileService.execute(request: DetectFileRequest) -> Result[DetectFileResponse]` as the only detect-file service API.

- [ ] Remove the feature-local `DocumentInput` import/export and the `to_domain()` dependency from `DetectFileRequest`.
- [ ] Move `_DetectFileCore.execute` logic into a private method on `DetectFileService`.
- [ ] Remove `_DetectFileCore` and `DetectFileAction` while preserving constructor options and `Result` error mapping.
- [ ] Remove `DetectFileAction` from `data_extraction.detect_file` exports.

### Task 2: Migrate the inspect feature caller

**Files:**
- Modify: `data_extraction/inspect_document/service.py`

**Interfaces:**
- Consumes: `DetectFileService.execute(DetectFileRequest)` and `DetectFileRequest.from_domain(DocumentInput)`.
- Produces: `InspectFileAction.execute` continues returning `InspectedDocument` and raises `ExtractionError` for detection failures.

- [ ] Replace the `DetectFileAction` dependency with `DetectFileService`.
- [ ] Convert the existing `DocumentInput` to `DetectFileRequest` before calling the detector.
- [ ] Translate a failed detect-file `Result` back into `ExtractionError` so existing inspect error handling remains intact.

### Task 3: Update stale feature references

**Files:**
- Modify: `data_extraction/detect_file/tests/test_detect_file.py`

**Interfaces:**
- Consumes: `DetectFileService` and `DetectFileRequest`.
- Produces: Existing detection examples target the canonical service contract.

- [ ] Replace imports and calls to `DetectFileAction`/feature-local `DocumentInput` with `DetectFileService`/`DetectFileRequest`.
- [ ] Assert success through `Result.unwrap()` and failure through the returned `Result` error rather than the removed exception facade.

## Execution note

The user explicitly requested direct implementation without running tests, so the implementation will be followed by static reference checks only.
