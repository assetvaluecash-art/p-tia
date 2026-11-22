"""Helpers for Requirement entity value/description separation in deterministic IE."""

from __future__ import annotations

import re
from typing import Any

_DESCRIPTION_COL_PATTERNS = re.compile(
    r"(?<![\w-])description(?![\w-])|requirement[\s_-]?text|requirement[\s_-]?statement|"
    r"requirement[\s_-]?desc|statement|detail|summary|requirement[\s_-]?details",
    re.IGNORECASE,
)
_ID_COL_PATTERNS = re.compile(
    r"\bid\b|identifier|ref[\s_-]?no|number",
    re.IGNORECASE,
)


def parse_kv_pairs(text: str) -> dict[str, str]:
    """Parse Key=Value or Key: Value pairs from a table row text."""
    pairs: dict[str, str] = {}
    for segment in re.split(r",\s*(?=[A-Za-z][A-Za-z0-9 _-]*[:=])", text):
        key = ""
        val = ""
        if "=" in segment:
            key, _, val = segment.partition("=")
        elif ":" in segment:
            key, _, val = segment.partition(":")
        else:
            continue
        key = key.strip()
        val = val.strip()
        if key and val:
            pairs[key] = val
    return pairs


def extract_requirement_description_from_text(text: str, req_value: str) -> str | None:
    """Derive human-readable requirement text from block text, keeping ID in value."""
    block = (text or "").strip()
    value = (req_value or "").strip()
    if not block:
        return None

    if value:
        id_prefix = re.compile(
            rf"^\s*{re.escape(value)}\s*[:\-–—]\s*(.+)$",
            re.IGNORECASE | re.DOTALL,
        )
        match = id_prefix.match(block)
        if match:
            desc = match.group(1).strip()
            if desc and desc.lower() != value.lower():
                return desc[:2000]

        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            line_match = re.match(
                rf"^\s*{re.escape(value)}\s*[:\-–—]\s*(.+)$",
                line,
                re.IGNORECASE,
            )
            if line_match:
                desc = line_match.group(1).strip()
                if desc and desc.lower() != value.lower():
                    return desc[:2000]

        redacted = re.sub(
            re.escape(value),
            " ",
            block,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        redacted = re.sub(r"^[:\-–—\s]+", "", redacted).strip()
        if len(redacted) >= 20 and redacted.lower() != value.lower():
            return redacted[:2000]

    lowered = block.lower()
    if lowered.startswith("system shall") or lowered.startswith("the system shall"):
        return block[:2000]

    return None



_DESCRIPTION_PREFIX_RE = re.compile(
    r"^\s*(?:description|details?|summary|statement|requirement|rule|control|regulation|"
    r"acceptance criteria|user story)\s*[:\-–—]\s*",
    re.IGNORECASE,
)


def extract_entity_description_from_text(
    text: str, entity_type: str, value: str
) -> str | None:
    """Return source text explicitly associated with an entity identity.

    This intentionally avoids using an arbitrary surrounding paragraph as a
    description. Type-specific semantics are supplied in the extraction prompt;
    this helper only handles explicit ID/title prefixes and labeled fields.
    """
    block = (text or "").strip()
    identity = (value or "").strip()
    if not block or not identity:
        return None

    for line in block.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        match = re.match(
            rf"^\s*{re.escape(identity)}\s*[:\-–—]\s*(.+)$",
            candidate,
            re.IGNORECASE,
        )
        if match and match.group(1).strip().lower() != identity.lower():
            return match.group(1).strip()[:2000]
        labeled = _DESCRIPTION_PREFIX_RE.match(candidate)
        if labeled:
            description = candidate[labeled.end():].strip()
            if description and description.lower() != identity.lower():
                return description[:2000]

    if entity_type.lower() == "requirement":
        return extract_requirement_description_from_text(block, identity)
    return None


def description_from_kv_pairs(kv: dict[str, str]) -> str | None:
    """Return description text from a structured table row's KV columns."""
    for col_name, col_val in kv.items():
        val = str(col_val or "").strip()
        if not val:
            continue
        if _ID_COL_PATTERNS.search(col_name) and not _DESCRIPTION_COL_PATTERNS.search(col_name):
            continue
        if _DESCRIPTION_COL_PATTERNS.search(col_name):
            return val[:2000]
    return None


def entity_with_optional_fields(
    entity_type: str,
    value: str,
    confidence: float,
    *,
    description: str | None = None,
    aliases: list[str] | None = None,
) -> dict[str, Any]:
    """Build a rule-based entity dict preserving optional description/aliases."""
    entity: dict[str, Any] = {
        "type": entity_type,
        "value": value,
        "confidence": confidence,
    }
    if description and description.strip():
        entity["description"] = description.strip()[:2000]
    if aliases:
        cleaned = [str(a).strip() for a in aliases if str(a).strip()]
        if cleaned:
            entity["aliases"] = cleaned[:10]
    return entity
