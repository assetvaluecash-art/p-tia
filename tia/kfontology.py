"""TIA's software ontology and the TIA <-> Knowledge Fabric bridge ontology, in Knowledge Fabric's canonical shape (docs/KF-BRIDGE.md
section 5).

Knowledge Fabric 2.5 describes its business graph in `config/canonical/model.yaml`: `node_types` (each with a `layer`) and
`relationship_types` (each with `allowed_triples`). This module publishes TIA's graph the same way, so the two can sit side by side:

  software ontology   generated from tia.graphschema (schema gs-3) plus what the Neo4j mirror adds (written test cases, the derived IMPACTS
                      edges). Labels are the Neo4j mirror's: `Tia<Type>`, so they never collide with Knowledge Fabric's `System`, `TestCase`, ...
  bridge ontology     the only relationships allowed between the two graphs (version kb-3). Each says how it is decided and what evidence
                      status it carries. A relationship with `written: false` is reported, not stored in Neo4j; the writer refuses any
                      triple not listed here as written.

TIA's internal relationships run between `TiaNode`s only and are marked `tia: true`; two of them share a name with a Knowledge Fabric
relationship (`IMPACTS`, `COVERS`) but never touch a Knowledge Fabric node. The bridge relationships use names Knowledge Fabric does not use
(`REALIZES`, `SAME_AS`, `STORES`, `ENFORCES`, `PLACED_IN`) and carry `bridge: true, owner: "tia"`. `PLACED_IN` is not `PART_OF` on purpose:
Knowledge Fabric walks `PART_OF` to build and use its own business tree, its path renderer hides `BELONGS_TO` as plumbing, and its vision
ontology uses `SUPPORTS` between sub-functions.
"""
from __future__ import annotations

import re

from .graphschema import EDGE_TYPES, GRAPH_SCHEMA_VERSION, NODE_TYPES

BRIDGE_VERSION = "kb-3"
LAYERS = {"code": "software_code", "data": "software_data", "events": "software_events", "automation": "software_ui",
          "api": "software_api", "rules": "software_rules"}
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def label_of(node_type: str) -> str:
    """TIA node type -> Neo4j label: endpoint -> TiaEndpoint, event_schema -> TiaEventSchema."""
    lab = "Tia" + "".join(p[:1].upper() + p[1:] for p in re.split(r"[^A-Za-z0-9]+", str(node_type or "unknown")) if p)
    return lab if NAME.match(lab) else "TiaUnknown"


# ---- the bridge ---------------------------------------------------------------------------------------------------------------------------
BRIDGE = [
    {"name": "REALIZES", "from": "TiaService", "to": "System", "planned": False,
     "decided_by": "declared alias (tenant setting knowledge_fabric.system_aliases, or business_systems in tia-services.yaml); otherwise the "
                   "exact Knowledge Fabric key of the service or repository name",
     "evidence": {"declared": "contract_confirmed", "name": "inferred (INF-KF-NAME)"},
     "description": "The service's code realises the named system (ArchiMate realization: an application component realises a "
                    "business-facing system)."},
    {"name": "SAME_AS", "from": "TiaTestCase", "to": "TestCase", "planned": False,
     "decided_by": "the same test case id under Knowledge Fabric's normalisation", "evidence": {"test_case_id": "source_confirmed"},
     "description": "The written test case in a repository and the Knowledge Fabric test case are the same test case."},
    {"name": "REALIZES", "from": "TiaScreen", "to": "FormCapture", "planned": False,
     "decided_by": "declared alias (knowledge_fabric.screen_aliases by route or component, or business_screens in tia-services.yaml); "
                   "otherwise the exact Knowledge Fabric key of the component or of the route's last segment",
     "evidence": {"declared": "contract_confirmed", "name": "inferred (INF-KF-NAME)"},
     "description": "The screen realises the form or screen the documents describe."},
    {"name": "STORES", "from": "TiaEntity", "to": "DataEntity", "planned": False,
     "decided_by": "declared alias (knowledge_fabric.data_aliases, or business_data in tia-services.yaml); otherwise the exact key of the "
                   "class name, or of the class name without a technical suffix (Record, Entity, Model, Dto, View, Row)",
     "evidence": {"declared": "contract_confirmed", "name": "inferred (INF-KF-NAME)"},
     "description": "The ORM entity stores the business data entity."},
    {"name": "STORES", "from": "TiaTable", "to": "DataEntity", "planned": False,
     "decided_by": "declared alias; otherwise the exact key of the table name or of its singular",
     "evidence": {"declared": "contract_confirmed", "name": "inferred (INF-KF-NAME)"},
     "description": "The table stores the business data entity."},
    {"name": "ENFORCES", "from": "TiaRule", "to": "Rule", "planned": False,
     "decided_by": "a person confirms a pair TIA proposed (shared field names, then word overlap); rule names are sentences, so names alone "
                   "never link", "evidence": {"confirmed": "contract_confirmed"},
     "description": "The rule found in code enforces the business rule."},
]
for _level in ("ProductLine", "Function", "SubFunction"):
    BRIDGE.append(
        {"name": "PLACED_IN", "from": "TiaService", "to": _level, "planned": False,
         "decided_by": "a person: tenant setting knowledge_fabric.repository_products / service_products, business_products in "
                       "tia-services.yaml, or a reviewer accepting a suggestion (a suggestion from the linked Systems is never applied by itself)",
         "evidence": {"declared": "contract_confirmed", "reviewed": "contract_confirmed"},
         "description": f"The service belongs to this {_level} of the customer's business tree (domain > product line > function > "
                        "sub-function); every impact of the service rolls up the tree from here. Carries product_line_id, because one "
                        "function can sit under two product lines."})
BRIDGE += [
    {"name": "IMPLEMENTS_CHANGE", "from": "TiaChange", "to": "UserStory", "planned": False, "written": False,
     "decided_by": "an issue key (PAY-4521) in a commit message of the change that Knowledge Fabric keeps as the user story's name or alias",
     "evidence": {"issue_key": "source_confirmed"},
     "description": "The change implements the user story. Reported in the analysis (business_impact.user_stories), not stored in Neo4j: "
                    "a change is an analysis, not a node of the mirror."},
]
ALLOWED = frozenset((b["from"], b["name"], b["to"]) for b in BRIDGE if not b["planned"] and b.get("written", True))
BRIDGE_NAMES = frozenset(b["name"] for b in BRIDGE)


def allowed(src_label: str, rel: str, dst_label: str) -> bool:
    return (src_label, rel, dst_label) in ALLOWED


# ---- the software ontology ----------------------------------------------------------------------------------------------------------------
def software_ontology() -> dict:
    """TIA's graph as an ontology in Knowledge Fabric's canonical shape."""
    nodes = []
    for t, n in NODE_TYPES.items():
        layer = "software_test" if t == "test" else LAYERS.get(n["group"], "software_code")
        nodes.append({"name": label_of(t), "tia_type": t, "layer": layer, "description": n["description"],
                      "properties": list(n.get("properties", [])) + list(n.get("optional", []))})
    nodes.append({"name": "TiaTestCase", "tia_type": "test_case", "layer": "software_test",
                  "description": "A written (manual) test case kept in a repository (testcases/*.csv|yaml|json).",
                  "properties": ["case_id", "title", "entry_point", "priority", "steps", "expected", "repo", "file", "line"]})
    rels = []
    for r, e in EDGE_TYPES.items():
        rels.append({"name": r, "allowed_triples": [{"from": label_of(a), "to": label_of(b)} for a in sorted(e["from"]) for b in sorted(e["to"])],
                     "tia_impact": e["impact"], "description": e["description"]})
    rels.append({"name": "COVERS", "allowed_triples": [{"from": "TiaTestCase", "to": "TiaEndpoint"}, {"from": "TiaTestCase", "to": "TiaScreen"}],
                 "tia_impact": None, "description": "A written test case tests the endpoint or screen it names (Neo4j mirror only)."})
    rels.append({"name": "IMPACTS", "allowed_triples": [{"from": "TiaNode", "to": "TiaNode"}], "tia_impact": "src->dst",
                 "description": "Every traversed relationship again, in the direction a change travels (Neo4j mirror only; `via` names it)."})
    return {"name": "tia-software", "version": GRAPH_SCHEMA_VERSION, "lane": "software", "domain_neutral": True,
            "description": "TIA's software graph: services, endpoints, code, data, events, screens, API structure, tests and rules found in "
                           "code. Not tied to a business domain; scoped by TIA tenant.",
            "node_types": nodes, "relationship_types": rels}


def bridge_ontology() -> dict:
    """The relationships allowed between TIA's software graph and Knowledge Fabric's business graph."""
    return {"name": "tia-kf-bridge", "version": BRIDGE_VERSION, "lane": "bridge",
            "description": "The only relationships between TIA's software graph and Knowledge Fabric's business graph. Always from a TIA node "
                           "to a Knowledge Fabric node, confined to one Knowledge Fabric customer; carry bridge=true, owner=tia, the "
                           "evidence status and both sides' versions.",
            "edge_properties": ["bridge", "owner", "tenant", "kf_customer_id", "kf_tenant_id", "domain", "status", "strategy", "rule", "source",
                                "id_check", "tia_graph_version", "kf_fingerprint", "bridge_version", "linked_at", "product_line",
                                "product_line_id", "level", "decided_by", "decided_at"],
            "relationship_types": [{"name": b["name"], "allowed_triples": [{"from": b["from"], "to": b["to"]}], "planned": b["planned"],
                                    "written": b.get("written", True), "decided_by": b["decided_by"], "evidence": b["evidence"],
                                    "description": b["description"]}
                                   for b in BRIDGE]}


def ontologies() -> dict:
    return {"software": software_ontology(), "bridge": bridge_ontology()}


def to_yaml(doc: dict) -> str:
    import yaml
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=140)
