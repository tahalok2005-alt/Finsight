"""Domain error hierarchy.  The API layer maps these onto HTTP status codes."""
from __future__ import annotations


class FinSightError(Exception):
    """Base class for all expected FinSight failures."""
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ValidationFailure(FinSightError):
    status_code = 422
    code = "validation_failed"


class UnsupportedFileType(FinSightError):
    status_code = 415
    code = "unsupported_file_type"


class FileTooLarge(FinSightError):
    status_code = 413
    code = "file_too_large"


class CorruptDocument(FinSightError):
    status_code = 422
    code = "corrupt_document"


class EncryptedDocument(FinSightError):
    status_code = 422
    code = "encrypted_document"


class EmptyDocument(FinSightError):
    status_code = 422
    code = "empty_document"


class DocumentNotFound(FinSightError):
    status_code = 404
    code = "document_not_found"


class ResourceNotFound(FinSightError):
    status_code = 404
    code = "not_found"


class ConflictError(FinSightError):
    status_code = 409
    code = "conflict"


class EngineUnavailable(FinSightError):
    status_code = 503
    code = "engine_unavailable"


class StageFailure(FinSightError):
    """A pipeline stage failed; carries the stage name for the run record."""
    status_code = 500
    code = "stage_failure"

    def __init__(self, stage: str, message: str, *, detail: dict | None = None):
        super().__init__(message, detail={**(detail or {}), "stage": stage})
        self.stage = stage
