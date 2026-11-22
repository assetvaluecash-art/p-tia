# tests/test_kg_esg_policy.py

from pathlib import Path
import yaml
import pytest
from neo4j import GraphDatabase


# ---------- config loading ----------

def _load_neo4j_config():
    """
    Load Neo4j connectivity from ../config/kbfabric.yaml (or kbfabri.yaml).
    """
    project_root = Path(__file__).resolve().parents[1]
    candidates = [
        project_root / "config" / "kbfabric.yaml",
        project_root / "config" / "kbfabri.yaml",
    ]

    for path in candidates:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            break
    else:
        raise FileNotFoundError(
            "Could not find kbfabric.yaml or kbfabri.yaml under ../config"
        )

    if "neo4j" not in cfg:
        raise KeyError("Config file is missing top level key 'neo4j'")

    return cfg["neo4j"]


# ---------- pytest fixtures ----------

@pytest.fixture(scope="session")
def neo4j_config():
    return _load_neo4j_config()


@pytest.fixture(scope="session")
def neo4j_driver(neo4j_config):
    uri = neo4j_config["uri"]
    user = neo4j_config["user"]
    password = neo4j_config["password"]

    driver = GraphDatabase.driver(uri, auth=(user, password))
    yield driver
    driver.close()


@pytest.fixture
def neo4j_session(neo4j_driver):
    with neo4j_driver.session() as session:
        yield session


def _single_int(session, query, **params):
    """
    Run a Cypher query that returns a single integer in the first column.
    """
    record = session.run(query, **params).single()
    if record is None:
        return 0
    value = record[0]
    return int(value) if value is not None else 0


# ---------- KG tests for ESG PDF ----------

def test_esg_process_nodes_exist(neo4j_session):
    """
    The ESG specific Process nodes created from the PDF should exist.
    """
    process_ids = [
        "PROC-ESG-DRIFT",
        "PROC-ESG-CONSTRAINT",
        "PROC-ESG-PROPOSAL",
        "PROC-ESG-COMPLIANCE",
        "PROC-ESG-EXEC",
        "PROC-ESG-REPORT",
    ]

    for pid in process_ids:
        count = _single_int(
            neo4j_session,
            """
            MATCH (p:Process {id: $pid})
            RETURN count(p) AS c
            """,
            pid=pid,
        )
        assert count > 0, f"Expected Process node with id={pid}, found {count}"


def test_esg_control_protects_execution(neo4j_session):
    """
    Volatility guardrail control should protect ESG execution process.
    """
    count = _single_int(
        neo4j_session,
        """
        MATCH (c:Control {id: 'CTRL-ESG-REBAL-002'})
              -[r:CONTROL_PROTECTS_PROCESS]->
              (p:Process {id: 'PROC-ESG-EXEC'})
        RETURN count(r) AS c
        """,
    )
    assert count > 0, (
        "Expected CONTROL_PROTECTS_PROCESS from CTRL-ESG-REBAL-002 "
        "to PROC-ESG-EXEC"
    )


def test_esg_negative_test_covers_execution(neo4j_session):
    """
    Negative test case for high volatility ESG behaviour should cover execution.
    """
    count = _single_int(
        neo4j_session,
        """
        MATCH (q:TestCase {id: 'TC-ESG-REBAL-NEG-001'})
              -[r:QE_ASSET_COVERS_PROCESS]->
              (p:Process {id: 'PROC-ESG-EXEC'})
        RETURN count(r) AS c
        """,
    )
    assert count > 0, (
        "Expected QE_ASSET_COVERS_PROCESS from TC-ESG-REBAL-NEG-001 "
        "to PROC-ESG-EXEC"
    )


def test_market_and_esg_data_feed_drift_detection(neo4j_session):
    """
    Market Data Load and ESG Data Load should both feed ESG Drift Detection.
    Relationship type can be any, we only assert the presence of a link.
    """
    source_names = [
        "Process Market Data Load",
        "Process ESG Data Load",
    ]

    for name in source_names:
        count = _single_int(
            neo4j_session,
            """
            MATCH (src:Process {name: $src_name})
                  -[r]->(dst:Process {id: 'PROC-ESG-DRIFT'})
            RETURN count(r) AS c
            """,
            src_name=name,
        )
        assert count > 0, (
            f"Expected at least one relationship from '{name}' "
            "to PROC-ESG-DRIFT"
        )
