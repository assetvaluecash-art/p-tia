"""TIA's review items in Knowledge Fabric's review screen (docs/KF-BRIDGE.md section 12, phase 2).

Knowledge Fabric 2.5 cannot take these items yet: its review queue is filled only by its own ingestion pipeline, its queue types are fixed
(NODE_ADMISSION, CONTEXT_RESOLUTION, UNRESOLVED_TARGET) and an approval runs its graph writer, which refuses anything that is not one of its
ontology types. This module is TIA's half of the contract asked of Knowledge Fabric; it is tested against a stand-in service that implements
that contract, not against Knowledge Fabric:

  TIA -> Knowledge Fabric
    POST {TIA_KF_REVIEW_URL}/hitl/reviews                                  create one SOFTWARE_LINK item (kfreview.inbox_payload); idempotent
                                                                           on plan_id (TIA's item id): a repeat answers 200/409 with the same
                                                                           review_id. -> {"review_id"}
    POST {TIA_KF_REVIEW_URL}/hitl/reviews/{review_id}/external-decision    close an item decided (or no longer asked) in TIA, with the scope
         ?ingestion_batch_id=&domain_id=&product_line_id=                  query Knowledge Fabric's review routes always require
         {"action": APPROVE_REUSE | APPROVE_REUSE_WITH_ALIAS | REJECT | WITHDRAWN, "selected_entity_id", "reviewer_id": "tia:<actor>",
          "notes", "decided_at"}
    Authorization: Bearer {TIA_KF_REVIEW_TOKEN}
  Knowledge Fabric -> TIA
    POST /api/v1/webhooks/kf-review   a reviewer's decision, signed: X-KF-Signature: sha256=<hex HMAC-SHA256 of the body with
                                      TIA_KF_REVIEW_WEBHOOK_SECRET>. Body: {"event_id", "review_id", "plan_id", "ingestion_batch_id",
                                      "customer_id", "action", "selected_entity_id", "reviewer_id", "notes", "decided_at"}

TIA keeps the decision (evidence kf_link_decision); Knowledge Fabric's table holds a copy for display. The first decision recorded in TIA
wins; a later one from the other screen is refused (HTTP 409).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.parse
from typing import Optional

from . import netsafe

BATCH = re.compile(r"^tia-links:([0-9a-fA-F-]{36}):(.+)$")
KF_ACTIONS = ("APPROVE_REUSE", "APPROVE_REUSE_WITH_ALIAS", "APPROVE_AS_NEW", "REJECT", "LEAVE_UNRESOLVED")


class KfInboxError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def config_from_env() -> Optional[dict]:
    """{"url", "token", "webhook_secret", "private_ok"} when TIA_KF_REVIEW_URL is set, else None (the inbox is off)."""
    url = (os.environ.get("TIA_KF_REVIEW_URL") or "").strip().rstrip("/")
    if not url:
        return None
    return {"url": url, "token": os.environ.get("TIA_KF_REVIEW_TOKEN") or None,
            "webhook_secret": os.environ.get("TIA_KF_REVIEW_WEBHOOK_SECRET") or None,
            "private_ok": os.environ.get("TIA_KF_REVIEW_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes")}


def webhook_secret() -> Optional[str]:
    return os.environ.get("TIA_KF_REVIEW_WEBHOOK_SECRET") or None


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(secret: Optional[str], body: bytes, header: Optional[str]) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(sign(secret, body), header.strip().lower())


def scope_of(batch_id: str) -> Optional[tuple[str, str]]:
    """`tia-links:<tenant>:<product_line_id>` -> (TIA tenant, product line id), or None."""
    m = BATCH.match(batch_id or "")
    return (m.group(1).lower(), m.group(2)) if m else None


def parse_callback(body: bytes) -> dict:
    """A signed decision from Knowledge Fabric's review screen, checked for shape. Raises ValueError."""
    try:
        d = json.loads(body)
    except ValueError:
        raise ValueError("the body is not JSON")
    if not isinstance(d, dict):
        raise ValueError("the body must be an object")
    for k in ("review_id", "plan_id", "ingestion_batch_id", "action", "reviewer_id"):
        if not isinstance(d.get(k), str) or not d[k].strip() or len(d[k]) > 300:
            raise ValueError(f"{k} is required (text of at most 300 characters)")
    if d["action"] not in KF_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(KF_ACTIONS)}")
    if scope_of(d["ingestion_batch_id"]) is None:
        raise ValueError("ingestion_batch_id is not a TIA review batch (tia-links:<tenant>:<product line>)")
    if d.get("selected_entity_id") is not None and (not isinstance(d["selected_entity_id"], str) or len(d["selected_entity_id"]) > 300):
        raise ValueError("selected_entity_id must be a text")
    notes = d.get("notes")
    return {"event_id": str(d.get("event_id") or "")[:200] or None, "review_id": d["review_id"], "plan_id": d["plan_id"],
            "batch": d["ingestion_batch_id"], "customer_id": d.get("customer_id"), "action": d["action"],
            "selected_entity_id": d.get("selected_entity_id"), "reviewer_id": re.sub(r"[^A-Za-z0-9@._ -]", "", d["reviewer_id"])[:64] or "unknown",
            "notes": str(notes)[:1000] if notes else None, "decided_at": d.get("decided_at")}


class KfReviewClient:
    """Calls Knowledge Fabric's review service, through TIA's outbound guard (air gap, private addresses, size and time limits)."""

    def __init__(self, url: str, token: Optional[str] = None, private_ok: bool = False):
        u = urllib.parse.urlsplit(url or "")
        if u.scheme not in ("https", "http") or not u.hostname:
            raise KfInboxError("TIA_KF_REVIEW_URL must be an http(s) URL of Knowledge Fabric's ingestion service", permanent=True)
        if u.scheme == "http" and not private_ok:
            raise KfInboxError("TIA_KF_REVIEW_URL must use https (http only with TIA_KF_REVIEW_ALLOW_PRIVATE inside a private network)",
                               permanent=True)
        self.base, self.token, self.private_ok = url.rstrip("/"), token, private_ok

    def _call(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "User-Agent": "tia"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            status, raw = netsafe.request(method, self.base + path, data=json.dumps(body).encode(), headers=headers, private_ok=self.private_ok)
        except netsafe.FetchError as e:
            raise KfInboxError(f"Knowledge Fabric's review service could not be reached: {e}")
        try:
            data = json.loads(raw or b"null") or {}
        except ValueError:
            data = {}
        if status >= 400 and status != 409:
            raise KfInboxError(f"Knowledge Fabric's review service answered HTTP {status}: {str(data)[:200]}",
                               permanent=status < 500 and status not in (408, 429))
        return status, data if isinstance(data, dict) else {}

    def create(self, payload: dict) -> str:
        """-> Knowledge Fabric's review id. A repeat (same plan_id) returns the existing one."""
        _, data = self._call("POST", "/hitl/reviews", payload)
        rid = data.get("review_id")
        if not isinstance(rid, str) or not rid:
            raise KfInboxError("Knowledge Fabric's review service did not return a review_id", permanent=True)
        return rid

    def close(self, review_id: str, scope: dict, decision: dict) -> None:
        q = urllib.parse.urlencode({k: scope[k] for k in ("ingestion_batch_id", "domain_id", "product_line_id")})
        self._call("POST", f"/hitl/reviews/{urllib.parse.quote(review_id, safe='')}/external-decision?{q}", decision)
