"""Knowledge Fabric 2.5's node identity, reproduced exactly (docs/KF-BRIDGE.md section 6).

Knowledge Fabric names every graph node by a deterministic id:

    entity_id = sha256(scope + ":" + domain + ":" + EntityType + ":" + normalized_name)
    scope     = "tenant:{T}|customer:{C}"  (or "customer:{C}" / "tenant:{T}" alone)

and builds `normalized_name` from the extracted text in three steps: sanitise the display value, apply a type-aware Title Case, then
lower-case and strip articles and punctuation. Source (KFabric-Ingestion-V2): `ingest/ingestion_metadata.py` (`customer_scope_id`,
`scoped_entity_id`) and `ingest/kg_writer.py` (`_sanitize_entity_display_value`, `_normalize_to_camelcase`, `_normalize_entity_value`).

TIA computes the same ids so it can find the Knowledge Fabric node for a service or a test case without searching by text. A computed id is
a first guess, not a guarantee: Knowledge Fabric's reconciliation may pin an entity to an existing node, so the bridge always checks the id
exists and otherwise looks the node up by name and aliases within the customer and domain (tia.kfbridge).
`tests/test_kfbridge.py::test_identity_matches_knowledge_fabrics_own_code` compares these functions with Knowledge Fabric's own on a corpus
of names whenever its source is present, so a change on their side fails a test before it can mislink anything.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

DISPLAY_MAX_LEN = 80                                        # kg_writer.KGWriter._ENTITY_DISPLAY_MAX_LEN
ID_TYPES = ("TestCase", "Defect", "Incident")             # kept verbatim as identifiers
SENTENCE_TYPES = ("UserStory", "Requirement", "AcceptanceCriteria")   # kept as sentences


def sanitize(value) -> Optional[str]:
    """kg_writer._sanitize_entity_display_value: None for a value Knowledge Fabric would drop."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if len(raw) < 2 or "=" in raw:
        return None
    if len(raw) > DISPLAY_MAX_LEN:
        raw = raw[:DISPLAY_MAX_LEN].rstrip()
        if len(raw) < 2:
            return None
    if not re.search(r"[a-zA-Z0-9]", raw):
        return None
    parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return raw if len(raw) >= 2 else None
    if any(len(p) == 1 for p in parts):
        return None
    best = max((p for p in parts if len(p) >= 2), key=len, default=None)
    return best if best else None


def display(entity_type: str, name: str) -> str:
    """kg_writer._normalize_to_camelcase: the name Knowledge Fabric stores, for this entity type."""
    if not name:
        return ""
    name = name.strip()
    if entity_type in ID_TYPES:
        if re.match(r"^[A-Za-z0-9_\-\.]+$", name) and len(name) <= 80:
            return name
        return " ".join(name.split())[:80]
    if entity_type in SENTENCE_TYPES:
        return " ".join(name.split())[:80]
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    words = spaced.replace("-", " ").replace("_", " ").split()
    return " ".join(w.capitalize() for w in words if w)[:80]


def normalized(value: str) -> str:
    """kg_writer._normalize_entity_value: the form that goes into the id."""
    if not value:
        return ""
    v = value.strip().lower()
    v = re.sub(r"^(the|a|an)\s+", "", v)
    v = re.sub(r"\s+(the|a|an)$", "", v)
    v = re.sub(r'[.,;:!?"\(\)\[\]{}]', "", v)
    return " ".join(v.split())


def key(entity_type: str, name: str) -> Optional[str]:
    """The normalized key of a name as Knowledge Fabric would store it for this type (sanitise -> display -> normalise); None if dropped."""
    s = sanitize(name)
    if s is None:
        return None
    return normalized(display(entity_type, s)) or None


def scope(customer_id: Optional[str], tenant_id: Optional[str] = None) -> str:
    """ingestion_metadata.customer_scope_id."""
    customer = str(customer_id).strip() if customer_id else None
    tenant = str(tenant_id).strip() if tenant_id else None
    if not customer and not tenant:
        raise ValueError("A customer_id or tenant_id is required for persisted identity")
    if customer and tenant:
        return f"tenant:{tenant}|customer:{customer}"
    return f"customer:{customer}" if customer else f"tenant:{tenant}"


def entity_id(scope_id: str, domain: str, entity_type: str, normalized_value: str) -> str:
    """ingestion_metadata.scoped_entity_id."""
    return hashlib.sha256(":".join((scope_id, domain, entity_type, normalized_value)).encode()).hexdigest()


def entity_id_for(customer_id: str, tenant_id: Optional[str], domain: str, entity_type: str, name: str) -> Optional[str]:
    """The id Knowledge Fabric gives `name` of `entity_type` in this customer and domain (before any reconciliation); None if dropped."""
    k = key(entity_type, name)
    return entity_id(scope(customer_id, tenant_id), domain, entity_type, k) if k else None
