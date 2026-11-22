"""Shared exceptions and API error contract for the ingestion platform."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PathSecurityError(Exception):
    """Raised when a file path violates security constraints."""

    pass


@dataclass(frozen=True)
class ApiError:
    """Platform-standard API error."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class PlatformApiError(Exception):
    """Base exception for errors exposed through the platform API."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class ValidationApiError(PlatformApiError):
    """Request failed platform validation."""

    def __init__(
        self,
        message: str = "Request validation failed.",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="VALIDATION_ERROR",
            message=message,
            status_code=400,
            details=details,
        )


class ResourceNotFoundApiError(PlatformApiError):
    """Requested resource was not found."""

    def __init__(
        self,
        message: str = "The requested resource was not found.",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="RESOURCE_NOT_FOUND",
            message=message,
            status_code=404,
            details=details,
        )


class CustomerNotFoundApiError(PlatformApiError):
    """Customer was not found."""

    def __init__(
        self,
        message: str = "Customer not found.",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="CUSTOMER_NOT_FOUND",
            message=message,
            status_code=404,
            details=details,
        )


class ResourceConflictApiError(PlatformApiError):
    """Request conflicts with the current resource state."""

    def __init__(
        self,
        message: str = "The request conflicts with the current state of the resource.",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="RESOURCE_CONFLICT",
            message=message,
            status_code=409,
            details=details,
        )


class DependencyFailureApiError(PlatformApiError):
    """A dependent service or resource failed."""

    def __init__(
        self,
        message: str = "A dependent service failed.",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="DEPENDENCY_FAILURE",
            message=message,
            status_code=502,
            details=details,
        )


class ServiceUnavailableApiError(PlatformApiError):
    """Service is temporarily unavailable."""

    def __init__(
        self,
        message: str = "The service is temporarily unavailable.",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="SERVICE_UNAVAILABLE",
            message=message,
            status_code=503,
            details=details,
        )


class InternalServerApiError(PlatformApiError):
    """Unexpected internal server failure."""

    def __init__(
        self,
        message: str = "An unexpected error occurred.",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="INTERNAL_SERVER_ERROR",
            message=message,
            status_code=500,
            details=details,
        )
