from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException
import jwt

from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class AuthenticatedActor:
    """Actor identity resolved from the trusted gateway token or dev test token."""

    user_id: str
    auth_source: str
    permissions: frozenset[str] = frozenset()


def extract_claims(token: str) -> dict:
    """Decode JWT claims without signature verification.

    Kong validates the JWT before the request reaches this service.
    """
    if not token:
        return {}

    try:
        clean = token.replace("Bearer ", "").strip()
        return jwt.decode(clean, options={"verify_signature": False})
    except Exception as exc:
        logger.warning("auth.jwt_decode_failed", error=str(exc))
        return {}


def get_user_id(token: str) -> str | None:
    """Extract the authenticated user identity from the Kong JWT."""
    clean = token.replace("Bearer ", "").strip()

    if clean.startswith("test-"):
        return clean

    claims = extract_claims(token)
    user_id = claims.get("userId") or claims.get("user_id")

    if not user_id:
        return None

    return str(user_id)


def require_authenticated_actor(
    authorization: str = Header(..., alias="Authorization"),
) -> AuthenticatedActor:
    """Resolve the actor supplied by Kong or an explicit development test token.

    JWT signature verification is delegated to Kong as documented in
    ``extract_claims``. Deployments must keep this service behind that gateway.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    clean = authorization.removeprefix("Bearer ").strip()
    user_id = get_user_id(authorization)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Authenticated user identity required",
        )

    auth_source = "dev_test_token" if clean.startswith("test-") else "kong_jwt"
    claims = extract_claims(authorization) if auth_source == "kong_jwt" else {}
    raw_permissions = claims.get("permissions", [])
    if isinstance(raw_permissions, str):
        permissions = frozenset(item for item in raw_permissions.split() if item)
    elif isinstance(raw_permissions, list):
        permissions = frozenset(
            item.strip() for item in raw_permissions
            if isinstance(item, str) and item.strip()
        )
    else:
        permissions = frozenset()
    logger.info(
        "auth.actor.resolved",
        user_id=user_id,
        auth_source=auth_source,
    )
    return AuthenticatedActor(
        user_id=user_id, auth_source=auth_source, permissions=permissions
    )


def require_user_id(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    """Backward-compatible dependency returning only the authenticated actor ID."""
    return require_authenticated_actor(authorization).user_id
