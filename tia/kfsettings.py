"""The `knowledge_fabric` tenant setting: defaults and validation (docs/KF-BRIDGE.md section 7).

In the rules layer so the tenant configuration (tia.tenantconfig) can validate it; the bridge itself (tia.kfbridge) plans the links.
"""
from __future__ import annotations

import copy
import re
from typing import Optional

from . import kfidentity as kid

DEFAULTS = {"enabled": False, "customer_id": None, "kf_tenant_id": None, "domains": "auto", "system_aliases": {}, "link_name_matches": True,
            "auto_relink": True, "poll_minutes": 15, "max_context_items": 100,
            # phase 7 (docs/KF-BRIDGE.md section 10): screens -> FormCapture, ORM entities and tables -> DataEntity, code rules -> Rule
            # (proposed, a person confirms), Jira-style issue keys in commit messages -> UserStory
            "screen_aliases": {}, "data_aliases": {}, "propose_rules": True, "link_issue_keys": True,
            # product mapping (docs/KF-BRIDGE.md section 11): where a repository or a service sits in Knowledge Fabric's business tree,
            # domain > product line > function > sub-function; chosen by a person, never guessed from code
            "repository_products": {}, "service_products": {},
            # review of uncertain links (section 12): one person decides, or two different people; and where reviewers work
            "review_policy": "single", "review_inbox": "tia", "review_inbox_detail": "minimal"}
ALIAS_SETTINGS = {"system_aliases": ("System", "a TIA service"), "screen_aliases": ("FormCapture", "a screen route or component"),
                  "data_aliases": ("DataEntity", "an ORM entity or table name")}
PRODUCT_SETTINGS = {"repository_products": "a repository", "service_products": "a service"}
CHOICES = {"review_policy": ("single", "two_person"), "review_inbox": ("tia", "tia_and_kf"), "review_inbox_detail": ("minimal", "full")}
KEYS = tuple(DEFAULTS)
MAX_ALIAS_SERVICES, MAX_ALIASES = 2000, 20
MAX_PLACES = 5
PLACE_FIELDS = ("domain", "product_line", "function", "sub_function")
# an alias value may name a Knowledge Fabric node by its entity id instead of its name: it settles a name that matches two nodes
ID_ALIAS = re.compile(r"^id:([0-9a-f]{64})$")
SETTABLE = tuple(k for k in KEYS if k not in ALIAS_SETTINGS)        # PATCH /kf/settings "set": every key but the alias maps


def alias_entity_id(value) -> Optional[str]:
    """`id:<64 hex>` -> the entity id; anything else -> None (a name)."""
    m = ID_ALIAS.match(value.strip()) if isinstance(value, str) else None
    return m.group(1) if m else None


def alias_ok(label: str, value) -> bool:
    return isinstance(value, str) and (alias_entity_id(value) is not None or bool(kid.key(label, value)))


def place(raw) -> Optional[dict]:
    """One place in the business tree, normalised: {"domain", "product_line"[, "function"[, "sub_function"]]}; None when malformed."""
    if not isinstance(raw, dict) or set(raw) - set(PLACE_FIELDS):
        return None
    out = {}
    for k in PLACE_FIELDS:
        v = raw.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or not v.strip() or len(v.strip()) > 200:
            return None
        out[k] = " ".join(v.split())
    if not out.get("domain") or not out.get("product_line") or ("sub_function" in out and "function" not in out):
        return None
    return out


def validate(raw, problems: list) -> Optional[dict]:
    """The `knowledge_fabric` tenant setting -> normalised dict (defaults filled in), appending every problem found."""
    if not isinstance(raw, dict):
        problems.append("knowledge_fabric must be an object")
        return None
    out = dict(DEFAULTS, system_aliases={}, screen_aliases={}, data_aliases={}, repository_products={}, service_products={})
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
                    not all(alias_ok(label, n) for n in names):
                problems.append(f"knowledge_fabric.{setting}.{src} must be a list of 1-{MAX_ALIASES} {label} names "
                                "(each at least 2 characters, no '=') or entity ids written id:<64 hex characters>")
            else:
                out[setting][str(src)] = [n.strip() for n in names]
    for setting, what in PRODUCT_SETTINGS.items():
        if setting not in raw:
            continue
        m = raw[setting]
        if not isinstance(m, dict) or len(m) > MAX_ALIAS_SERVICES:
            problems.append(f"knowledge_fabric.{setting} must map {what} to a list of places in Knowledge Fabric's business tree "
                            f"(at most {MAX_ALIAS_SERVICES} entries)")
            continue
        for name, places in m.items():
            if isinstance(places, dict):
                places = [places]
            ok = isinstance(places, list) and 1 <= len(places) <= MAX_PLACES
            norm = [place(p) for p in places] if ok else []
            if not ok or any(p is None for p in norm) or not str(name).strip():
                problems.append(f"knowledge_fabric.{setting}.{name} must be a list of 1-{MAX_PLACES} places, each "
                                '{"domain", "product_line"[, "function"[, "sub_function"]]} (texts of at most 200 characters; '
                                "a sub_function needs its function)")
            else:
                out[setting][str(name).strip()] = [dict(t) for t in dict.fromkeys(tuple(sorted(p.items())) for p in norm)]
    for k, allowed in CHOICES.items():
        if k in raw:
            if raw[k] in allowed:
                out[k] = raw[k]
            else:
                problems.append(f"knowledge_fabric.{k} must be one of: {', '.join(allowed)}")
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


def merge(current: Optional[dict], set_: Optional[dict] = None, add_aliases: Optional[list] = None,
          remove_aliases: Optional[list] = None) -> tuple[dict, list]:
    """A partial change to the raw `knowledge_fabric` setting (PATCH /api/v1/kf/settings) -> (new raw setting, problems with the request).
    set_: top-level keys to replace (not the alias maps); add_aliases / remove_aliases: [{"setting", "from", "to": [names]}]. Removing the
    last name of an entry removes the entry. The result still has to pass validate()."""
    out = copy.deepcopy(current or {})
    problems: list = []
    for k, v in (set_ or {}).items():
        if k not in SETTABLE:
            problems.append(f"'{k}' cannot be set here" + (": use add_aliases / remove_aliases" if k in ALIAS_SETTINGS else
                                                         " (not a knowledge_fabric setting)"))
        else:
            out[k] = v
    for op, entries in (("add", add_aliases or []), ("remove", remove_aliases or [])):
        for i, e in enumerate(entries):
            setting, src, to = (e or {}).get("setting"), (e or {}).get("from"), (e or {}).get("to")
            to = [to] if isinstance(to, str) else to
            if setting not in ALIAS_SETTINGS or not isinstance(src, str) or not src.strip() or not isinstance(to, list) or \
                    not all(isinstance(x, str) and x.strip() for x in to):
                problems.append(f"{op}_aliases[{i}] must be {{\"setting\": one of {', '.join(ALIAS_SETTINGS)}, \"from\": a name, "
                                "\"to\": [Knowledge Fabric names or id:<entity id>]}")
                continue
            al = dict(out.get(setting) or {})
            cur = al.get(src.strip()) or []
            cur = [cur] if isinstance(cur, str) else list(cur)
            want = [x.strip() for x in to]
            if op == "add":
                cur += [x for x in want if x not in cur]
            else:
                cur = [x for x in cur if x not in want]
            if cur:
                al[src.strip()] = cur
            else:
                al.pop(src.strip(), None)
            out[setting] = al
    return out, problems


def canonical(settings: Optional[dict]) -> dict:
    """The user-facing form of the setting (defaults filled in), as TenantConfig.to_dict shows it."""
    s = dict(DEFAULTS, **(settings or {}))
    return {"enabled": bool(s["enabled"]), "customer_id": s["customer_id"], "kf_tenant_id": s["kf_tenant_id"], "domains": s["domains"],
            "system_aliases": dict(sorted((s["system_aliases"] or {}).items())), "link_name_matches": bool(s["link_name_matches"]),
            "auto_relink": bool(s["auto_relink"]), "poll_minutes": s["poll_minutes"], "max_context_items": s["max_context_items"],
            "screen_aliases": dict(sorted((s["screen_aliases"] or {}).items())),
            "data_aliases": dict(sorted((s["data_aliases"] or {}).items())),
            "propose_rules": bool(s["propose_rules"]), "link_issue_keys": bool(s["link_issue_keys"]),
            "repository_products": dict(sorted((s["repository_products"] or {}).items())),
            "service_products": dict(sorted((s["service_products"] or {}).items())),
            "review_policy": s["review_policy"], "review_inbox": s["review_inbox"], "review_inbox_detail": s["review_inbox_detail"]}
