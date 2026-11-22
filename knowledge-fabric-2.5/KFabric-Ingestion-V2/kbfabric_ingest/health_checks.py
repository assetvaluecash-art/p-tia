"""Shared connectivity checks for Weaviate and Neo4j.

Used by the ingestion API readiness probe and by the ingestion job pre-flight
so both fail fast with consistent behavior. Config is passed in (from
get_app_config()); env overrides are applied at config load time.
"""

from typing import Any

import weaviate
from neo4j import GraphDatabase


# Timeouts for connectivity checks (seconds)
WEAVIATE_CHECK_TIMEOUT = (10, 10)
NEO4J_CONNECTION_TIMEOUT = 10


def check_weaviate(config: dict[str, Any]) -> tuple[bool, str | None]:
    """
    Verify Weaviate is reachable and responsive.

    Args:
        config: Full app config (e.g. from get_app_config()).

    Returns:
        (True, None) if healthy, (False, error_message) otherwise.
    """
    weaviate_cfg = config["weaviate"]
    url = weaviate_cfg["url"]
    try:
        client = weaviate.Client(
            url,
            auth_client_secret=weaviate.AuthApiKey(weaviate_cfg["api_key"]) if weaviate_cfg.get("api_key") else None,
            timeout_config=WEAVIATE_CHECK_TIMEOUT,
        )
        client.schema.get()
        return (True, None)
    except Exception as e:
        return (False, str(e)[:200])


def check_neo4j(config: dict[str, Any]) -> tuple[bool, str | None]:
    """
    Verify Neo4j is reachable and responsive.

    Args:
        config: Full app config (e.g. from get_app_config()).

    Returns:
        (True, None) if healthy, (False, error_message) otherwise.
    """
    neo4j_cfg = config["neo4j"]
    uri = neo4j_cfg["uri"]
    driver = None
    try:
        driver = GraphDatabase.driver(
            uri,
            auth=(neo4j_cfg["user"], neo4j_cfg["password"]),
            connection_timeout=NEO4J_CONNECTION_TIMEOUT,
        )
        with driver.session() as session:
            session.run("RETURN 1").consume()
        return (True, None)
    except Exception as e:
        return (False, str(e)[:200])
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass
