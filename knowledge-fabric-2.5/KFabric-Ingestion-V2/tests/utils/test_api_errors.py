from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError

from kbfabric_ingest.utils.api_errors import (
    ApiErrorResponse,
    api_error_response,
    http_exception,
    validation_error_response,
)


def test_api_error_response_shape():
    result = api_error_response(
        code="RESOURCE_CONFLICT",
        message="The request conflicts with the current state of the resource.",
        details={
            "requested_version": 3,
            "current_version": 4,
        },
    )

    assert result == {
        "code": "RESOURCE_CONFLICT",
        "message": "The request conflicts with the current state of the resource.",
        "details": {
            "requested_version": 3,
            "current_version": 4,
        },
    }


def test_api_error_response_omits_empty_details():
    result = api_error_response(
        code="CUSTOMER_NOT_FOUND",
        message="Customer not found.",
    )

    assert result == {
        "code": "CUSTOMER_NOT_FOUND",
        "message": "Customer not found.",
    }


def test_http_exception_uses_standard_error_payload():
    error = http_exception(
        status_code=409,
        code="RESOURCE_CONFLICT",
        message="The request conflicts with the current state of the resource.",
        details={"current_version": 4},
    )

    assert isinstance(error, HTTPException)
    assert error.status_code == 409
    assert error.detail == {
        "code": "RESOURCE_CONFLICT",
        "message": "The request conflicts with the current state of the resource.",
        "details": {"current_version": 4},
    }


def test_validation_error_response_maps_fields():
    errors = [
        {
            "type": "string_too_short",
            "loc": ("body", "customer_id"),
            "msg": "String should have at least 1 character",
            "input": "",
        }
    ]

    request_error = RequestValidationError(errors)

    result = validation_error_response(request_error)

    assert result["code"] == "VALIDATION_ERROR"
    assert result["message"] == "Request validation failed."
    assert result["details"]["fields"][0]["field"] == "body.customer_id"
