from __future__ import annotations

from typing import Any


class AppError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "application_error",
        status_code: int = 400,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details


class NotFoundError(AppError):
    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message, code="not_found", status_code=404, details=details)


class ConflictError(AppError):
    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message, code="conflict", status_code=409, details=details)

