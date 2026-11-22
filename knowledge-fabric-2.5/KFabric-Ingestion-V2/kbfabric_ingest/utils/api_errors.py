"""Platform-standard API error response models and exception handling."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field


class ApiErrorResponse(BaseModel):
    """Standard error response returned by platform APIs."""

    code: str = Field(
        ...,
        description="Platform error code from the shared error catalogue.",
        examples=["RESOURCE_CONFLICT"],
    )
    message: str = Field(
        ...,
        description="Human-readable error message suitable for display or logging.",
        examples=["The request conflicts with the current state of the resource."],
    )
    details: dict[str, Any] | None = Field(
        None,
        description="Structured details that allow the UI to handle the error.",
    )


def api_error_response(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a platform-standard error payload."""

    return ApiErrorResponse(
        code=code,
        message=message,
        details=details,
    ).model_dump(exclude_none=True)


def http_exception(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> HTTPException:
    """Create a FastAPI HTTPException using the platform error contract."""

    return HTTPException(
        status_code=status_code,
        detail=api_error_response(
            code=code,
            message=message,
            details=details,
        ),
    )


def validation_error_response(exc: RequestValidationError) -> dict[str, Any]:
    """Convert FastAPI/Pydantic validation errors to the platform 400 contract."""

    details = {
        "fields": [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
    }

    return api_error_response(
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        details=details,
    )
