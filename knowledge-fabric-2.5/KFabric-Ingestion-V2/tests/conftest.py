"""Pytest configuration and fixtures for KBFabricKB tests."""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Generator

import pytest
import requests
import weaviate
from neo4j import GraphDatabase

from kbfabric_ingest.ingest.config import get_app_config

# DIAGNOSTIC: This should print immediately when conftest.py is imported
print("[CONFTEST] conftest.py module imported successfully", flush=True)


def pytest_configure(config):
    """Called after command line options have been parsed."""
    print("\n[PYTEST HOOK] pytest_configure called - configuration complete", flush=True)


def pytest_addoption(parser):
    """Add custom pytest command-line options."""
    parser.addoption(
        "--skip-service-checks",
        action="store_true",
        default=False,
        help="Skip health checks for external services (Weaviate, Neo4j, Ollama)"
    )


def pytest_sessionstart(session):
    """Called after collection is complete, before test execution."""
    print("\n[PYTEST HOOK] pytest_sessionstart called - session setup beginning", flush=True)
    print(f"[PYTEST HOOK] Collected {len(session.items)} test items", flush=True)


def run_with_timeout(func, timeout_sec: float, *args, **kwargs):
    """
    Run a function with a hard timeout using ThreadPoolExecutor.
    
    Args:
        func: Function to execute
        timeout_sec: Maximum time to wait in seconds
        *args, **kwargs: Arguments to pass to func
        
    Returns:
        Result of func(*args, **kwargs)
        
    Raises:
        TimeoutError: If function doesn't complete within timeout_sec
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout_sec)
        except FutureTimeoutError:
            raise TimeoutError(f"Operation timed out after {timeout_sec} seconds")


def check_weaviate_health(config: dict) -> tuple[bool, str]:
    """Check if Weaviate is accessible and healthy."""
    url = config.get("url", "http://localhost:8080")
    api_key = config.get("api_key")
    
    def _check():
        try:
            # Initialize client with timeout to prevent hanging (10 second timeout)
            # For weaviate-client v3, timeout_config is a tuple: (query_timeout, insert_timeout)
            timeout_config = (10, 10)  # 10 second timeout for both query and insert
            
            if api_key:
                try:
                    client = weaviate.Client(url, auth_client_secret=weaviate.AuthApiKey(api_key), timeout_config=timeout_config)
                except TypeError:
                    # Fallback if timeout_config not supported in this version
                    client = weaviate.Client(url, auth_client_secret=weaviate.AuthApiKey(api_key))
            else:
                try:
                    client = weaviate.Client(url, timeout_config=timeout_config)
                except TypeError:
                    # Fallback if timeout_config not supported in this version
                    client = weaviate.Client(url)
            
            # Try to get schema (lightweight operation)
            client.schema.get()
            return True, f"Weaviate is healthy at {url}"
        except Exception as e:
            return False, f"Weaviate health check failed at {url}: {str(e)}"
    
    try:
        return run_with_timeout(_check, timeout_sec=10.0)
    except TimeoutError:
        return False, f"Weaviate health check timed out after 10 seconds at {url}"


def check_neo4j_health(config: dict) -> tuple[bool, str]:
    """Check if Neo4j is accessible and healthy."""
    uri = config.get("uri", "bolt://localhost:7687")
    user = config.get("user", "neo4j")
    password = config.get("password", "")
    
    def _check():
        try:
            # Initialize driver with timeout to prevent hanging (10 second timeout)
            try:
                driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=10.0)
            except TypeError:
                # Fallback if connection_timeout not supported in this version
                driver = GraphDatabase.driver(uri, auth=(user, password))
            
            with driver.session() as session:
                # Simple query to verify connection
                session.run("RETURN 1 as test")
            driver.close()
            return True, f"Neo4j is healthy at {uri}"
        except Exception as e:
            return False, f"Neo4j health check failed at {uri}: {str(e)}"
    
    try:
        return run_with_timeout(_check, timeout_sec=10.0)
    except TimeoutError:
        return False, f"Neo4j health check timed out after 10 seconds at {uri}"


def check_ollama_health(config: dict) -> tuple[bool, str]:
    """Check if Ollama is accessible and healthy."""
    base_url = config.get("base_url", "http://localhost:11434")
    timeout = 5  # Short timeout for health check
    
    try:
        # Check if Ollama API is responding
        response = requests.get(f"{base_url}/api/tags", timeout=timeout)
        response.raise_for_status()
        return True, f"Ollama is healthy at {base_url}"
    except requests.exceptions.RequestException as e:
        return False, f"Ollama health check failed at {base_url}: {str(e)}"


# DIAGNOSTIC: This should print when pytest processes the fixture definition
print("[CONFTEST] Defining check_external_services fixture", flush=True)

@pytest.fixture(scope="session", autouse=True)
def check_external_services(request):
    """Check external services for functional/integration tests only."""
    # Unit tests must remain isolated from external infrastructure.
    test_paths = [
        str(item.fspath)
        for item in request.session.items
    ]

    running_unit_tests = bool(test_paths) and all(
        "/tests/" in path and "/unit/" in path
        for path in test_paths
    )

    if running_unit_tests:
        print(
            "\n[HEALTH CHECK] Skipping external services for unit tests.",
            flush=True,
        )
        print(
            "[HEALTH CHECK] Unit tests are isolated from "
            "Weaviate, Neo4j, and Ollama.",
            flush=True,
        )
        yield
        return

    print("\n[HEALTH CHECK] Starting service health checks...", flush=True)

    skip_checks = request.config.getoption(
        "--skip-service-checks",
        default=False,
    )

    # Allow override via environment variable.
    if os.getenv("SKIP_SERVICE_CHECKS", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        skip_checks = True

    if skip_checks:
        print("[SKIP] Service health checks disabled", flush=True)
        yield
        return

    print("[HEALTH CHECK] Verifying external services...", flush=True)

    config = get_app_config()
    all_healthy = True
    errors = []

    # Check Weaviate.
    print("[HEALTH CHECK] Checking Weaviate...", flush=True)
    weaviate_config = config.get("weaviate", {})
    healthy, message = check_weaviate_health(weaviate_config)
    print(
        f"  {'[OK]' if healthy else '[FAIL]'} {message}",
        flush=True,
    )

    if not healthy:
        all_healthy = False
        errors.append(f"Weaviate: {message}")

    # Check Neo4j.
    print("[HEALTH CHECK] Checking Neo4j...", flush=True)
    neo4j_config = config.get("neo4j", {})
    healthy, message = check_neo4j_health(neo4j_config)
    print(
        f"  {'[OK]' if healthy else '[FAIL]'} {message}",
        flush=True,
    )

    if not healthy:
        all_healthy = False
        errors.append(f"Neo4j: {message}")

    # Check Ollama. It is optional.
    print("[HEALTH CHECK] Checking Ollama...", flush=True)
    ollama_config = config.get("ollama", {})
    healthy, message = check_ollama_health(ollama_config)
    print(
        f"  {'[OK]' if healthy else '[WARN]'} {message}",
        flush=True,
    )

    if not healthy:
        print(
            "  [WARNING] Ollama unavailable - "
            "LLM-dependent tests may fail",
            flush=True,
        )

    if not all_healthy:
        print(
            "\n[ERROR] Some required services are unavailable:",
            flush=True,
        )

        for error in errors:
            print(f"  - {error}", flush=True)

        print(
            "\n[HINT] Start services with: "
            "docker compose -f infra\\dev\\docker-compose.yml up -d",
            flush=True,
        )

        print(
            "[HINT] Or skip checks with: "
            "pytest --skip-service-checks",
            flush=True,
        )

        pytest.exit(
            "External service health checks failed",
            returncode=1,
        )

    print(
        "[HEALTH CHECK] All required services are healthy\n",
        flush=True,
    )

    yield


@pytest.fixture(scope="function")
def reset_datastores(request):
    """Optional fixture to reset datastores before tests."""
    # Check if reset is enabled via environment variable
    reset_enabled = os.getenv("RESET_DATASTORES_BEFORE_TESTS", "").lower() in ("1", "true", "yes")
    
    if reset_enabled:
        try:
            from kbfabric_ingest.infra.reset_datastores import reset_all
            print("\n[RESET] Clearing Weaviate and Neo4j before test...", flush=True)
            reset_all()
            print("[RESET] Datastores cleared\n", flush=True)
        except ImportError:
            print("[WARNING] reset_datastores module not available", flush=True)
        except Exception as e:
            print(f"[WARNING] Failed to reset datastores: {e}", flush=True)
    
    yield


