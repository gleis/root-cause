"""
Authentication + role-based access control.

Two credential types are accepted, either one satisfies `require_role`:

  1. OIDC JWTs (human/UI users) -- `Authorization: Bearer <jwt>`, signature
     verified against the IdP's JWKS (RS256), issuer/audience checked, a
     configurable claim (`OIDC_ROLES_CLAIM`, default "roles") mapped to an
     app role. See docs/auth-setup.md for how to register this app with a
     real IdP (Keycloak worked example; Okta/Azure AD notes).

  2. API keys (service-to-service: Alertmanager webhook, CI, scripts) --
     `Authorization: Bearer <key>` or the `X-API-Key` header. Validated
     against a SHA-256 hash stored in the `api_keys` table -- the plaintext
     key is never persisted anywhere and is only ever shown once, at
     creation time, by backend/scripts/create_api_key.py.

Design principle: FAIL CLOSED. Any missing, malformed, expired, or
unverifiable credential is a 401. A verified credential whose role doesn't
meet the endpoint's minimum is a 403. There is no code path that lets a
request through by default -- the only exception is the explicit,
opt-in-and-demo-mode-gated anonymous fallback below, which cannot be enabled
in a real deployment by itself (see Settings.auth_allow_anonymous_in_demo).
"""
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Request, status
import jwt
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from . import models

logger = logging.getLogger("rootcause.auth")

# Role hierarchy, lowest to highest privilege.
ROLE_ORDER = ["viewer", "engineer", "admin"]


def _role_rank(role: str) -> int:
    try:
        return ROLE_ORDER.index(role)
    except ValueError:
        return -1  # unknown role -> lowest possible rank, fails every check


@dataclass
class Actor:
    id: str            # OIDC `sub` claim, or api_keys.id
    type: str           # "oidc_user" | "api_key" | "system"
    role: str
    description: str = ""


# ---------------------------------------------------------------------------
# OIDC JWT verification
# ---------------------------------------------------------------------------

_jwks_cache: dict = {"keys": None, "fetched_at": 0.0}


def _get_jwks(settings) -> dict:
    now = time.monotonic()
    if _jwks_cache["keys"] is not None and (now - _jwks_cache["fetched_at"]) < settings.oidc_jwks_cache_seconds:
        return _jwks_cache["keys"]

    jwks_url = settings.oidc_jwks_url or (settings.oidc_issuer_url.rstrip("/") + "/.well-known/jwks.json")
    if not jwks_url:
        raise RuntimeError("OIDC_JWKS_URL / OIDC_ISSUER_URL not configured")

    resp = httpx.get(jwks_url, timeout=5.0)
    resp.raise_for_status()
    keys = resp.json()
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys


def _map_role_from_claim(claim_value) -> Optional[str]:
    """
    `claim_value` may be a list of role/group strings or a single string,
    depending on IdP configuration. We look for an exact match against an
    app role name, or a "rootcause-<role>" prefixed group name (the pattern
    documented in docs/auth-setup.md for IdPs where groups are shared across
    multiple apps). If more than one app role is present, the highest-
    privilege one wins (so a user in both "rootcause-viewer" and
    "rootcause-admin" groups gets admin).
    """
    if claim_value is None:
        return None
    values = claim_value if isinstance(claim_value, (list, tuple)) else [claim_value]

    best = None
    for v in values:
        if not isinstance(v, str):
            continue
        candidate = v.lower()
        if candidate.startswith("rootcause-"):
            candidate = candidate[len("rootcause-"):]
        if candidate in ROLE_ORDER:
            if best is None or _role_rank(candidate) > _role_rank(best):
                best = candidate
    return best


def verify_oidc_token(token: str) -> Actor:
    settings = get_settings()
    if not settings.oidc_issuer_url or not settings.oidc_audience:
        raise HTTPException(status_code=401, detail="OIDC is not configured on this server")

    try:
        jwks = _get_jwks(settings)
        unverified_header = jwt.get_unverified_header(token)
        if unverified_header.get("alg") != "RS256":
            raise HTTPException(status_code=401, detail="Unsupported token algorithm")
        key = None
        for k in jwks.get("keys", []):
            if k.get("kid") == unverified_header.get("kid"):
                key = k
                break
        if key is None:
            # kid rotated since our cache was populated -- force one refetch
            # before giving up, then fail closed.
            _jwks_cache["keys"] = None
            jwks = _get_jwks(settings)
            for k in jwks.get("keys", []):
                if k.get("kid") == unverified_header.get("kid"):
                    key = k
                    break
        if key is None:
            raise HTTPException(status_code=401, detail="No matching JWKS key for token")

        if (key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256"
                or key.get("use", "sig") != "sig"
                or "verify" not in key.get("key_ops", ["verify"])):
            raise HTTPException(status_code=401, detail="Invalid signing key")
        public_key = jwt.PyJWK.from_dict(key, algorithm="RS256").key
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer_url,
        )
    except HTTPException:
        raise
    except InvalidTokenError as exc:
        logger.info("OIDC token rejected: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
    except Exception as exc:  # noqa: BLE001 -- any failure here must fail closed
        logger.warning("OIDC verification error: %s", exc)
        raise HTTPException(status_code=401, detail="Unable to verify token") from exc

    role = _map_role_from_claim(claims.get(settings.oidc_roles_claim))
    if role is None:
        raise HTTPException(status_code=403, detail="Token has no recognized role claim")

    subject = claims.get("sub") or claims.get("email") or "unknown-subject"
    return Actor(id=str(subject), type="oidc_user", role=role, description=claims.get("email", ""))


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def verify_api_key(db: Session, plaintext: str) -> Actor:
    key_hash = hash_api_key(plaintext)
    row = db.query(models.ApiKey).filter_by(key_hash=key_hash).first()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if row.revoked_at is not None:
        raise HTTPException(status_code=401, detail="API key has been revoked")

    from datetime import datetime, timezone
    row.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return Actor(id=row.id, type="api_key", role=row.role, description=row.description or "")


# ---------------------------------------------------------------------------
# Combined dependency
# ---------------------------------------------------------------------------

def _extract_credential(request: Request, settings) -> Optional[str]:
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    api_key_header = request.headers.get(settings.api_key_header)
    if api_key_header:
        return api_key_header.strip()
    return None


def _looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2


def authenticate(request: Request, db: Session = Depends(get_db)) -> Actor:
    """Resolves the calling Actor from the request, or raises 401/403.
    Does NOT enforce a minimum role -- see require_role() for that. Stashes
    the resolved actor on request.state for audit logging."""
    settings = get_settings()
    credential = _extract_credential(request, settings)

    if credential is None:
        if settings.demo_mode and settings.auth_allow_anonymous_in_demo:
            actor = Actor(id="demo-anonymous", type="system", role="admin", description="DEMO_MODE anonymous access")
            request.state.actor = actor
            return actor
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials: supply 'Authorization: Bearer <token>' (OIDC JWT or API key) "
                   f"or '{settings.api_key_header}: <api key>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if _looks_like_jwt(credential):
        actor = verify_oidc_token(credential)
    else:
        actor = verify_api_key(db, credential)

    request.state.actor = actor
    return actor


def require_role(min_role: str):
    """FastAPI dependency factory. Usage: `Depends(require_role("engineer"))`.
    Accepts EITHER a valid OIDC JWT or a valid API key; enforces that the
    resolved actor's role meets or exceeds `min_role` in ROLE_ORDER."""
    min_rank = _role_rank(min_role)
    if min_rank < 0:
        raise ValueError(f"Unknown role: {min_role}")

    def _dependency(actor: Actor = Depends(authenticate)) -> Actor:
        if _role_rank(actor.role) < min_rank:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{actor.role}' does not meet required minimum '{min_role}'",
            )
        return actor

    return _dependency
