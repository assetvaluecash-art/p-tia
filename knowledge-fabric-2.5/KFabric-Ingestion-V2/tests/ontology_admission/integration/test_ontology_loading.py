"""
Integration tests for the Ontology Admission Engine's ontology loading.

WHAT THIS TESTS
---------------
Verifies that OntologyAdmissionEngine loads the production enterprise
banking ontology through the repository's existing get_ontology() mechanism
and exposes the authoritative node-type and relationship-type vocabulary.

WHY THIS MATTERS
----------------
The admission engine must not maintain a second hard-coded ontology.
enterprise_banking_ontology.yaml remains the single source of truth.

INPUTS
------
- config/enterprise_banking_ontology.yaml
- kbfabric_ingest.ingest.config.get_ontology()
- OntologyAdmissionEngine

EXPECTED BEHAVIOR
-----------------
- Core node types are discovered from the real ontology.
- Core relationship types are discovered from the real ontology.
- The admission engine does not require a separately maintained ontology
  vocabulary.

FAILURE MODES PROTECTED AGAINST
-------------------------------
- Wrong ontology being loaded.
- Stale or duplicated ontology vocabulary.
- Missing node types required by the admission engine.
- Missing relationship types required by the admission engine.
- Broken ontology loading.

TEST LEVEL
----------
Integration. This test intentionally uses the real repository ontology
rather than a mocked ontology.
"""

from kbfabric_ingest.ingest.config import get_ontology
from kbfabric_ingest.ingest.ontology_admission import OntologyAdmissionEngine


def test_admission_engine_loads_authoritative_node_and_relationship_vocabulary():
    """
    Verify that the admission engine exposes the ontology's authoritative
    node and relationship vocabulary.

    This establishes the first integration contract for the admission engine:
    candidate validation must be based on the same ontology used by the
    existing ingestion pipeline.
    """
    ontology = get_ontology()
    engine = OntologyAdmissionEngine(ontology)

    expected_node_types = {
        "Process",
        "System",
        "Actor",
        "Rule",
        "Control",
        "Regulation",
    }

    expected_relationship_types = {
        "USES",
        "PERFORMS",
        "GOVERNED_BY",
        "INTEGRATES_WITH",
    }

    assert expected_node_types.issubset(engine.node_types)
    assert expected_relationship_types.issubset(engine.relationship_types)
