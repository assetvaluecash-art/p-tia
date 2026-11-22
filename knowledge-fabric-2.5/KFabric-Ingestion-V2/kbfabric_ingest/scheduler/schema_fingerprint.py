"""
Schema fingerprint for detecting ontology/index config changes.

Single fabric-level fingerprint = hash(ontology, weaviate.index, neo4j.vector_index, schema_version).
Stored in Weaviate (SchemaMetadata class) and Neo4j (:SchemaMetadata node).
When stored != current and force_rewrite is false, job submit returns 409.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import weaviate
from neo4j import GraphDatabase

from kbfabric_ingest.ingest.config import ONTOLOGY_PATH, get_app_config
from kbfabric_ingest.ingest.weaviate_writer import SCHEMA_VERSION
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_METADATA_CLASS = "SchemaMetadata"
SCHEMA_METADATA_ID = "default"
WEAVIATE_TIMEOUT = (10, 10)


def _canonical_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Return a sorted, JSON-serializable copy for deterministic hashing."""
    if not d:
        return {}
    return json.loads(json.dumps(d, sort_keys=True))


def compute_fingerprint() -> str:
    """
    Compute current fabric fingerprint from ontology, weaviate.index, neo4j.vector_index, schema_version.

    Returns:
        SHA-256 hex string (deterministic for same inputs).
    """
    config = get_app_config()
    parts: list[bytes] = []

    # Include index config in hash so that changes to weaviate.index or neo4j.vector_index
    # require force_rewrite; otherwise the store would keep old index definitions and
    # behavior would be wrong (e.g. different dimensions or similarity metric).

    # Ontology file bytes (full content)
    if ONTOLOGY_PATH.exists():
        parts.append(ONTOLOGY_PATH.read_bytes())
    else:
        parts.append(b"")

    # weaviate.index (distance, ef_construction, max_connections, ef)
    wv_index = (config.get("weaviate") or {}).get("index") or {}
    parts.append(json.dumps(_canonical_dict(wv_index), sort_keys=True).encode("utf-8"))

    # neo4j.vector_index (optional; empty dict if not present)
    neo_index = (config.get("neo4j") or {}).get("vector_index") or {}
    parts.append(json.dumps(_canonical_dict(neo_index), sort_keys=True).encode("utf-8"))

    # Schema version
    parts.append(SCHEMA_VERSION.encode("utf-8"))

    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.hexdigest()


def _weaviate_client():
    """Create a minimal Weaviate client from config."""
    config = get_app_config()
    wv = config["weaviate"]
    url = wv["url"]
    api_key = wv.get("api_key") or None
    try:
        if api_key:
            return weaviate.Client(
                url,
                auth_client_secret=weaviate.AuthApiKey(api_key),
                timeout_config=WEAVIATE_TIMEOUT,
            )
        return weaviate.Client(url, timeout_config=WEAVIATE_TIMEOUT)
    except TypeError:
        if api_key:
            return weaviate.Client(url, auth_client_secret=weaviate.AuthApiKey(api_key))
        return weaviate.Client(url)


def _neo4j_driver():
    """Create Neo4j driver from config."""
    config = get_app_config()
    neo = config["neo4j"]
    uri = neo["uri"]
    user = neo["user"]
    password = neo["password"]
    return GraphDatabase.driver(uri, auth=(user, password), connection_timeout=10)


def get_stored_fingerprint() -> str | None:
    """
    Read the stored fingerprint from Weaviate (prefer) or Neo4j. Return None if missing or on error.
    """
    # Try Weaviate first
    try:
        client = _weaviate_client()
        if not client.schema.exists(SCHEMA_METADATA_CLASS):
            return None
        result = client.query.get(SCHEMA_METADATA_CLASS, ["fingerprint"]).with_where({
            "path": ["id"],
            "operator": "Equal",
            "valueString": SCHEMA_METADATA_ID,
        }).with_limit(1).do()
        data = result.get("data", {}).get("Get", {}).get(SCHEMA_METADATA_CLASS, [])
        if data and isinstance(data[0].get("fingerprint"), str):
            return data[0]["fingerprint"]
    except Exception as e:
        logger.warning("Failed to read fingerprint from Weaviate, trying Neo4j", error=str(e))

    # Fallback: Neo4j
    driver = None
    try:
        driver = _neo4j_driver()
        with driver.session() as session:
            r = session.run(
                "MATCH (s:SchemaMetadata {id: $id}) RETURN s.fingerprint AS fp",
                id=SCHEMA_METADATA_ID,
            )
            record = r.single()
            if record and record.get("fp"):
                return record["fp"]
    except Exception as e:
        logger.warning("Failed to read fingerprint from Neo4j", error=str(e))
    finally:
        if driver:
            try:
                driver.close()
            except Exception:
                pass
    return None


def _ensure_schema_metadata_class(client: weaviate.Client) -> None:
    """Create SchemaMetadata class in Weaviate if it does not exist."""
    if client.schema.exists(SCHEMA_METADATA_CLASS):
        return
    class_schema = {
        "class": SCHEMA_METADATA_CLASS,
        "description": "Fabric-level schema fingerprint for change detection",
        "vectorizer": "none",
        "properties": [
            {"name": "id", "dataType": ["string"], "description": "Singleton id"},
            {"name": "fingerprint", "dataType": ["string"], "description": "SHA-256 of ontology + index config"},
            {"name": "updated_at", "dataType": ["string"], "description": "ISO timestamp"},
        ],
    }
    client.schema.create_class(class_schema)
    logger.info("Created Weaviate class for schema fingerprint", class_name=SCHEMA_METADATA_CLASS)


def write_fingerprint(fingerprint: str) -> None:
    """
    Write fingerprint and updated_at to both Weaviate and Neo4j.
    On failure, log but do not raise (caller may still want to report success).
    """
    ts = datetime.now(timezone.utc).isoformat()

    # Weaviate: upsert single object (delete existing then create, or use put with id)
    try:
        client = _weaviate_client()
        _ensure_schema_metadata_class(client)
        # Weaviate v3: delete objects with id=default then create new one (no native upsert by content)
        try:
            client.batch.delete_objects(
                class_name=SCHEMA_METADATA_CLASS,
                where={"path": ["id"], "operator": "Equal", "valueString": SCHEMA_METADATA_ID},
            )
        except Exception:
            pass
        client.data_object.create(
            data_object={"id": SCHEMA_METADATA_ID, "fingerprint": fingerprint, "updated_at": ts},
            class_name=SCHEMA_METADATA_CLASS,
        )
        logger.info("Wrote schema fingerprint to Weaviate", fingerprint_prefix=fingerprint[:16])
    except Exception as e:
        logger.error("Failed to write fingerprint to Weaviate", error=str(e))

    # Neo4j
    driver = None
    try:
        driver = _neo4j_driver()
        with driver.session() as session:
            session.run(
                "MERGE (s:SchemaMetadata {id: $id}) SET s.fingerprint = $fp, s.updated_at = $ts",
                id=SCHEMA_METADATA_ID,
                fp=fingerprint,
                ts=ts,
            )
        logger.info("Wrote schema fingerprint to Neo4j", fingerprint_prefix=fingerprint[:16])
    except Exception as e:
        logger.error("Failed to write fingerprint to Neo4j", error=str(e))
    finally:
        if driver:
            try:
                driver.close()
            except Exception:
                pass
