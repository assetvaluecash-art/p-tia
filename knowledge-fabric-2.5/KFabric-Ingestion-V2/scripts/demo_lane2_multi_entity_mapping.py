#!/usr/bin/env python3
"""End-to-end demo: multi-entity Lane 2 classify → extract × N → shared mapping.

Uses scripted/mock LLM + in-memory Neo4j candidates (no live Neo4j required).
Shows every stage required by the multi-entity mapping meeting decisions.

Usage (from KFabric-Ingestion-V2 repo root):
  python scripts/demo_lane2_multi_entity_mapping.py
  python scripts/demo_lane2_multi_entity_mapping.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kbfabric_ingest.ingest.config import get_ontology
from kbfabric_ingest.ingest.entity_mapping import (
    MappingDecision,
    map_extracted_entity,
)
from kbfabric_ingest.ingest.lane2_node_reconciliation import (
    ExistingNode,
    MockSemanticSimilarityProvider,
)
from kbfabric_ingest.ingest.lane2_two_pass import (
    YamlOntologyProvider,
    process_lane2_block,
)
from kbfabric_ingest.ingest.models import CanonicalBlock


MULTI_BLOCK_TEXT = (
    "FR-04: The system shall auto-generate a repayment schedule upon loan booking. "
    "Loan disbursement initiation requires the maker to enter the instruction in LOS_Core "
    "and the checker to approve before funds are released. Dual control is mandatory: "
    "maker-checker separation is an operational control for disbursement. "
    "If the account reaches 90+ DPD, interest accrual ceases under the NAB classification rule."
)


class ScriptedLLM:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system_prompt: str, user_content: str) -> tuple[str, dict[str, Any]]:
        self.calls.append((system_prompt, user_content))
        if not self.responses:
            raise AssertionError("no scripted responses left")
        return self.responses.pop(0), {"duration_ms": 5.0, "token_usage": {"prompt": 1, "completion": 1}}


def _print(title: str, payload: Any) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit a single JSON document at the end")
    args = parser.parse_args()

    ontology = get_ontology()
    block = CanonicalBlock(
        block_id="demo-multi-1",
        source_file="demo/multi_entity.docx",
        file_type="docx",
        domain="corporate_commercial",
        page_number=1,
        block_index=0,
        block_type="paragraph",
        text=MULTI_BLOCK_TEXT,
        image_bytes=None,
        metadata={"heading_context_raw": "Lending / Disbursement"},
        semantic_unit_id="su-demo-multi",
        source_block_ids=["demo-multi-1"],
    )

    llm = ScriptedLLM(
        [
            json.dumps(
                {
                    "categories": [
                        {"category": "Requirement", "confidence": 0.93},
                        {"category": "Process", "confidence": 0.90},
                        {"category": "Control", "confidence": 0.86},
                        {"category": "Rule", "confidence": 0.84},
                    ],
                    "confidence": 0.93,
                    "unmatched": False,
                    "reasoning_summary": "Shall-requirement, disbursement process, dual control, and NAB rule.",
                }
            ),
            json.dumps(
                {
                    "entities": [
                        {
                            "type": "Requirement",
                            "value": "FR-04 Repayment Schedule",
                            "confidence": 0.95,
                        }
                    ],
                    "relationships": [
                        {
                            "from_type": "Requirement",
                            "from_value": "FR-04 Repayment Schedule",
                            "rel_type": "GOVERNED_BY",
                            "to_type": "Control",
                            "to_value": "Maker Checker Dual Control",
                            "confidence": 0.85,
                        }
                    ],
                    "confidence": 0.92,
                }
            ),
            json.dumps(
                {
                    "entities": [
                        {
                            "type": "Process",
                            "value": "Loan Disbursement Initiation",
                            "confidence": 0.94,
                        },
                        {"type": "System", "value": "LOS_Core", "confidence": 0.9},
                    ],
                    "relationships": [
                        {
                            "from_type": "Process",
                            "from_value": "Loan Disbursement Initiation",
                            "rel_type": "USES",
                            "to_type": "System",
                            "to_value": "LOS_Core",
                            "confidence": 0.9,
                        }
                    ],
                    "confidence": 0.91,
                }
            ),
            json.dumps(
                {
                    "entities": [
                        {
                            "type": "Control",
                            "value": "Maker Checker Dual Control",
                            "confidence": 0.93,
                        }
                    ],
                    "relationships": [],
                    "confidence": 0.9,
                }
            ),
            json.dumps(
                {
                    "entities": [
                        {
                            "type": "Rule",
                            "value": "NAB Classification at 90 DPD",
                            "confidence": 0.92,
                        }
                    ],
                    "relationships": [],
                    "confidence": 0.88,
                }
            ),
        ]
    )

    two_pass = process_lane2_block(
        block,
        llm_callable=llm,
        prompt_provider=YamlOntologyProvider(ontology),
        ontology=ontology,
        review_io=False,
    )

    _print("1. Original block", {"block_id": block.block_id, "text": block.text})
    _print(
        "2. Lane 2 multi-type classification",
        {
            "categories": [c.model_dump() for c in two_pass.classification.categories],
            "primary": two_pass.classification.category,
            "confidence": two_pass.classification.confidence,
        },
    )
    _print(
        "3–5. Type-specific extractions + constraints + merged result",
        {
            "extraction_calls": [
                {
                    "classified_node_type": e.classified_node_type,
                    "prompt_id": e.prompt_id,
                    "incoming_node_types": e.incoming_node_types,
                    "outgoing_node_types": e.outgoing_node_types,
                    "allowed_relationship_types": e.allowed_relationship_types,
                    "entities": e.entities,
                    "relationships": e.relationships,
                    "provenance_detail": e.provenance_detail.model_dump()
                    if e.provenance_detail
                    else None,
                }
                for e in two_pass.extractions
            ],
            "merged_entities": two_pass.extraction.entities if two_pass.extraction else [],
            "merged_relationships": two_pass.extraction.relationships
            if two_pass.extraction
            else [],
        },
    )

    # Seed graph: Requirement already exists; Control does not
    graph_seed = {
        "Requirement:FR-04 Repayment Schedule": ExistingNode(
            entity_id="neo4j-req-fr04",
            name="FR-04 Repayment Schedule",
            kg_node_type="Requirement",
            aliases=["FR-04"],
            description="Existing requirement node",
        ),
        "Process:Loan Disbursement Initiation": ExistingNode(
            entity_id="neo4j-proc-disb",
            name="Loan Disbursement Initiation",
            kg_node_type="Process",
        ),
        "System:LOS_Core": ExistingNode(
            entity_id="neo4j-sys-los",
            name="LOS_Core",
            kg_node_type="System",
        ),
    }

    mapping_rows: list[dict[str, Any]] = []
    semantic = MockSemanticSimilarityProvider()
    entities = list(two_pass.extraction.entities) if two_pass.extraction else []
    relationships = list(two_pass.extraction.relationships) if two_pass.extraction else []

    for idx, ent in enumerate(entities):
        key = f"{ent.get('type')}:{ent.get('value')}"
        existing = [graph_seed[key]] if key in graph_seed else []
        related_exists: dict[str, bool] = {}
        for rel in relationships:
            for side_type, side_val in (
                (rel.get("from_type"), rel.get("from_value")),
                (rel.get("to_type"), rel.get("to_value")),
            ):
                rk = f"{side_type}:{side_val}".lower()
                related_exists[rk] = f"{side_type}:{side_val}" in graph_seed

        mapped = map_extracted_entity(
            ent,
            lane="lane2",
            block=block,
            relationships=relationships,
            ontology=ontology,
            existing_candidates=existing,
            semantic=semantic,
            related_entity_exists=related_exists,
            index=idx,
        )
        mapping_rows.append(
            {
                "entity": ent,
                "candidates": [
                    {
                        "entity_id": c.candidate.entity_id,
                        "name": c.candidate.name,
                        "kg_node_type": c.candidate.kg_node_type,
                        "lexical_score": c.lexical_score,
                        "semantic_score": c.semantic_score,
                        "fusion_score": c.fusion_score,
                    }
                    for c in mapped.candidates
                ],
                "score_breakdown": mapped.score_breakdown.model_dump(),
                "mapping_decision": mapped.decision.value,
                "reused_node_id": mapped.selected_entity_id,
                "delta_nodes": mapped.delta_nodes,
                "delta_relationships": mapped.delta_relationships,
                "cypher_plan": mapped.cypher_plan,
                "provenance": mapped.provenance,
            }
        )

    _print("6–14. Existing-node mapping / delta decisions", mapping_rows)

    summary = {
        "classified_types": two_pass.classification.identified_categories,
        "extraction_calls": len(two_pass.extractions),
        "merged_entity_count": len(entities),
        "decisions": [r["mapping_decision"] for r in mapping_rows],
        "reused": [
            r["reused_node_id"] for r in mapping_rows if r["reused_node_id"]
        ],
        "delta_nodes": [n for r in mapping_rows for n in r["delta_nodes"]],
        "delta_relationships": [d for r in mapping_rows for d in r["delta_relationships"]],
    }
    _print("Summary", summary)

    # Expected story for the demo fixture
    assert "Requirement" in two_pass.classification.identified_categories
    assert len(two_pass.extractions) >= 3
    req_row = next(r for r in mapping_rows if r["entity"]["type"] == "Requirement")
    assert req_row["reused_node_id"] == "neo4j-req-fr04"
    assert req_row["mapping_decision"] in {
        MappingDecision.REUSE_EXISTING.value,
        MappingDecision.CREATE_DELTA_RELATIONSHIP.value,
        MappingDecision.CREATE_DELTA_NODE_AND_RELATIONSHIP.value,
        MappingDecision.NO_CHANGE.value,
    }
    control_row = next(r for r in mapping_rows if r["entity"]["type"] == "Control")
    assert control_row["mapping_decision"] == MappingDecision.CREATE_NEW.value

    if args.json:
        print(json.dumps({"two_pass": two_pass.model_dump(), "mapping": mapping_rows}, indent=2, default=str))

    print("\nDemo completed successfully (mock LLM + in-memory candidates).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
