from kbfabric_ingest.utils.exceptions import (
    CustomerNotFoundApiError,
    DependencyFailureApiError,
    InternalServerApiError,
    ResourceConflictApiError,
    ResourceNotFoundApiError,
    ServiceUnavailableApiError,
    ValidationApiError,
)


def test_platform_error_catalogue_mapping():
    cases = [
        (ValidationApiError(), "VALIDATION_ERROR", 400),
        (ResourceNotFoundApiError(), "RESOURCE_NOT_FOUND", 404),
        (CustomerNotFoundApiError(), "CUSTOMER_NOT_FOUND", 404),
        (ResourceConflictApiError(), "RESOURCE_CONFLICT", 409),
        (DependencyFailureApiError(), "DEPENDENCY_FAILURE", 502),
        (ServiceUnavailableApiError(), "SERVICE_UNAVAILABLE", 503),
        (InternalServerApiError(), "INTERNAL_SERVER_ERROR", 500),
    ]

    for error, expected_code, expected_status in cases:
        assert error.code == expected_code
        assert error.status_code == expected_status
        assert error.message
        assert str(error) == error.message


def test_platform_error_preserves_details():
    error = ResourceConflictApiError(
        details={
            "requested_version": 3,
            "current_version": 4,
        }
    )

    assert error.code == "RESOURCE_CONFLICT"
    assert error.details == {
        "requested_version": 3,
        "current_version": 4,
    }
