"""The `knowledge_fabric` tenant setting: defaults and validation (docs/KF-BRIDGE.md section 7).

In the rules layer so the tenant configuration (tia.tenantconfig) can validate it; the bridge itself (tia.kfbridge) plans the links.
"""
from __future__ import annotations

from typing import Optional

from . import kfidentity as kid

DEFAULTS = {"enabled": False, "customer_id": None, "kf_tenant_id": None, "domains": "auto", "system_aliases": {}, "link_name_matches": True,
            "auto_relink": True, "poll_minutes": 15, "max_context_items": 100,
            # phase 7 (docs/KF-BRIDGE.md section 10): screens -> FormCapture, ORM entities and tables -> DataEntity, code rules -> Rule
            # (proposed, a person confirms), Jira-style issue keys in commit messages -> UserStory
            "screen_aliases": {}, "data_aliases": {}, "propose_rules": True, "link_issue_keys": True}
ALIAS_SETTINGS = {"system_aliases": ("System", "a TIA service"), "screen_aliases": ("FormCapture", "a screen route or component"),
                  "data_aliases": ("DataEntity", "an ORM entity or table name")}
KEYS = tuple(DEFAULTS)
MAX_ALIAS_SERVICES, MAX_ALIASES = 2000, 20


def validate(raw, problems: list) -> Optional[dict]:
    """The `knowledge_fabric` tenant setting -> normalised dict (defaults filled in), appending every problem found."""
    if not isinstance(raw, dict):
        problems.append("knowledge_fabric must be an object")
        return None
    out = dict(DEFAULTS, system_aliases={}, screen_aliases={}, data_aliases={})
    for k in raw:
        if k not in KEYS:
            problems.append(f"unknown setting 'knowledge_fabric.{k}'")
    for k in ("enabled", "link_name_matches", "auto_relink", "propose_rules", "link_issue_keys"):
        if k in raw:
            if isinstance(raw[k], bool):
                out[k] = raw[k]
            else:
                problems.append(f"knowledge_fabric.{k} must be true or false")
    for k, limit in (("customer_id", 300), ("kf_tenant_id", 200)):
        v = raw.get(k)
        if v is not None:
            if not isinstance(v, str) or not v.strip() or len(v) > limit:
                problems.append(f"knowledge_fabric.{k} must be a non-empty text of at most {limit} characters")
            else:
                out[k] = v.strip()
    if "domains" in raw:
        d = raw["domains"]
        if d == "auto":
            out["domains"] = "auto"
        elif isinstance(d, list) and d and all(isinstance(x, str) and x.strip() and len(x) <= 120 for x in d) and len(d) <= 50:
            out["domains"] = sorted({x.strip() for x in d})
        else:
            problems.append('knowledge_fabric.domains must be "auto" or a list of Knowledge Fabric domain names (at most 50)')
    for setting, (label, what) in ALIAS_SETTINGS.items():
        if setting not in raw:
            continue
        al = raw[setting]
        if not isinstance(al, dict) or len(al) > MAX_ALIAS_SERVICES:
            problems.append(f"knowledge_fabric.{setting} must map {what} to a list of Knowledge Fabric {label} names "
                            f"(at most {MAX_ALIAS_SERVICES} entries)")
            continue
        for src, names in al.items():
            if isinstance(names, str):
                names = [names]
            if not isinstance(names, list) or not names or len(names) > MAX_ALIASES or \
                    not all(isinstance(n, str) and kid.key(label, n) for n in names):
                problems.append(f"knowledge_fabric.{setting}.{src} must be a list of 1-{MAX_ALIASES} {label} names "
                                "(each at least 2 characters, no '=')")
            else:
                out[setting][str(src)] = [n.strip() for n in names]
    if "poll_minutes" in raw:
        v = raw["poll_minutes"]
        if isinstance(v, bool) or not isinstance(v, int) or not 5 <= v <= 1440:
            problems.append("knowledge_fabric.poll_minutes must be a whole number of minutes from 5 to 1440")
        else:
            out["poll_minutes"] = v
    if "max_context_items" in raw:
        v = raw["max_context_items"]
        if isinstance(v, bool) or not isinstance(v, int) or not 10 <= v <= 500:
            problems.append("knowledge_fabric.max_context_items must be a whole number from 10 to 500")
        else:
            out["max_context_items"] = v
    if out["enabled"] and not out["customer_id"]:
        problems.append("knowledge_fabric.customer_id is required when knowledge_fabric.enabled is true "
                        "(pick it from GET /api/v1/kf/customers)")
    return out


def canonical(settings: Optional[dict]) -> dict:
    """The user-facing form of the setting (defaults filled in), as TenantConfig.to_dict shows it."""
    s = dict(DEFAULTS, **(settings or {}))
    return {"enabled": bool(s["enabled"]), "customer_id": s["customer_id"], "kf_tenant_id": s["kf_tenant_id"], "domains": s["domains"],
            "system_aliases": dict(sorted((s["system_aliases"] or {}).items())), "link_name_matches": bool(s["link_name_matches"]),
            "auto_relink": bool(s["auto_relink"]), "poll_minutes": s["poll_minutes"], "max_context_items": s["max_context_items"],
            "screen_aliases": dict(sorted((s["screen_aliases"] or {}).items())),
            "data_aliases": dict(sorted((s["data_aliases"] or {}).items())),
            "propose_rules": bool(s["propose_rules"]), "link_issue_keys": bool(s["link_issue_keys"])}
