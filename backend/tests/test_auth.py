"""
Auth enforcement tests: API keys (valid/invalid/revoked/insufficient role)
and OIDC JWT verification (valid/invalid signature/wrong issuer/wrong
audience/missing role claim), plus require_role()'s fail-closed behavior
end-to-end via FastAPI's TestClient.

No real IdP is used -- for the OIDC tests we generate our own RSA keypair,
sign a JWT with it, and monkeypatch app.auth._get_jwks to serve our own
public key as the "JWKS", which exercises the exact same verification code
path (`jwt.decode` against a JWKS-shaped key) a real IdP would.
"""
import time

import pytest
import jwt

from app import models
from app.auth import (
    Actor, hash_api_key, verify_api_key, verify_oidc_token, _role_rank, ROLE_ORDER,
)


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def _make_api_key(db_session, role="engineer", plaintext="rc_testkey1234567890"):
    row = models.ApiKey(key_hash=hash_api_key(plaintext), prefix=plaintext[:11], role=role, description="test key")
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return plaintext, row


def test_verify_api_key_success(db_session):
    plaintext, row = _make_api_key(db_session, role="engineer")
    actor = verify_api_key(db_session, plaintext)
    assert actor.type == "api_key"
    assert actor.role == "engineer"
    assert actor.id == row.id


def test_verify_api_key_invalid_rejected(db_session):
    _make_api_key(db_session)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        verify_api_key(db_session, "rc_not-a-real-key")
    assert exc_info.value.status_code == 401


def test_verify_api_key_revoked_rejected(db_session):
    plaintext, row = _make_api_key(db_session)
    from datetime import datetime, timezone
    row.revoked_at = datetime.now(timezone.utc)
    db_session.commit()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        verify_api_key(db_session, plaintext)
    assert exc_info.value.status_code == 401


def test_role_rank_ordering():
    assert _role_rank("viewer") < _role_rank("engineer") < _role_rank("admin")
    assert _role_rank("not-a-role") == -1


# ---------------------------------------------------------------------------
# OIDC JWTs (self-issued keypair standing in for a real IdP's JWKS)
# ---------------------------------------------------------------------------

@pytest.fixture()
def rsa_keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    public_numbers = key.public_key().public_numbers()
    from jwt.utils import to_base64url_uint as long_to_base64
    jwk = {
        "kty": "RSA",
        "kid": "test-kid",
        "use": "sig",
        "alg": "RS256",
        "n": long_to_base64(public_numbers.n).decode(),
        "e": long_to_base64(public_numbers.e).decode(),
    }
    return private_pem, {"keys": [jwk]}


def _issue_token(private_pem, claims, kid="test-kid"):
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


def _configure_oidc_settings(monkeypatch, jwks):
    """Points app.auth at a fake issuer/audience and short-circuits the
    JWKS HTTP fetch to return our locally-generated key set."""
    import app.auth as auth_mod

    base = auth_mod.get_settings()
    patched = base.model_copy(update={
        "oidc_issuer_url": "https://idp.example.com/realms/rootcause",
        "oidc_audience": "rootcause-api",
        "oidc_roles_claim": "roles",
    })
    monkeypatch.setattr(auth_mod, "get_settings", lambda: patched)
    monkeypatch.setattr(auth_mod, "_get_jwks", lambda settings: jwks)
    auth_mod._jwks_cache["keys"] = None
    return patched


def test_valid_oidc_token_maps_role(monkeypatch, rsa_keypair):
    private_pem, jwks = rsa_keypair
    settings = _configure_oidc_settings(monkeypatch, jwks)
    token = _issue_token(private_pem, {
        "sub": "user-123",
        "iss": settings.oidc_issuer_url,
        "aud": settings.oidc_audience,
        "roles": ["rootcause-engineer"],
        "exp": int(time.time()) + 3600,
    })
    actor = verify_oidc_token(token)
    assert actor.type == "oidc_user"
    assert actor.role == "engineer"
    assert actor.id == "user-123"


def test_oidc_token_wrong_audience_rejected(monkeypatch, rsa_keypair):
    private_pem, jwks = rsa_keypair
    settings = _configure_oidc_settings(monkeypatch, jwks)
    token = _issue_token(private_pem, {
        "sub": "user-123",
        "iss": settings.oidc_issuer_url,
        "aud": "some-other-app",
        "roles": ["admin"],
        "exp": int(time.time()) + 3600,
    })
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        verify_oidc_token(token)
    assert exc_info.value.status_code == 401


def test_oidc_token_expired_rejected(monkeypatch, rsa_keypair):
    private_pem, jwks = rsa_keypair
    settings = _configure_oidc_settings(monkeypatch, jwks)
    token = _issue_token(private_pem, {
        "sub": "user-123",
        "iss": settings.oidc_issuer_url,
        "aud": settings.oidc_audience,
        "roles": ["admin"],
        "exp": int(time.time()) - 10,
    })
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        verify_oidc_token(token)
    assert exc_info.value.status_code == 401


def test_oidc_token_missing_role_claim_rejected(monkeypatch, rsa_keypair):
    private_pem, jwks = rsa_keypair
    settings = _configure_oidc_settings(monkeypatch, jwks)
    token = _issue_token(private_pem, {
        "sub": "user-123",
        "iss": settings.oidc_issuer_url,
        "aud": settings.oidc_audience,
        "exp": int(time.time()) + 3600,
    })
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        verify_oidc_token(token)
    assert exc_info.value.status_code == 403


def test_oidc_token_wrong_signing_key_rejected(monkeypatch, rsa_keypair):
    """Signs with a DIFFERENT keypair than the one advertised in the JWKS --
    simulates a forged/tampered token. Must be rejected."""
    _, jwks = rsa_keypair
    settings = _configure_oidc_settings(monkeypatch, jwks)

    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged_pem = forged_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    token = _issue_token(forged_pem, {
        "sub": "attacker",
        "iss": settings.oidc_issuer_url,
        "aud": settings.oidc_audience,
        "roles": ["admin"],
        "exp": int(time.time()) + 3600,
    })
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        verify_oidc_token(token)
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# End-to-end via TestClient: require_role() fail-closed behavior
# ---------------------------------------------------------------------------

def test_missing_credentials_rejected_when_anonymous_disabled(client, db_session, monkeypatch):
    import app.auth as auth_mod
    base = auth_mod.get_settings()
    patched = base.model_copy(update={"auth_allow_anonymous_in_demo": False})
    monkeypatch.setattr(auth_mod, "get_settings", lambda: patched)

    resp = client.post("/api/v1/incidents", json={"title": "t", "service": "svc"})
    assert resp.status_code == 401


def test_invalid_api_key_rejected(client):
    resp = client.post(
        "/api/v1/incidents",
        json={"title": "t", "service": "svc"},
        headers={"Authorization": "Bearer rc_totally-invalid-key"},
    )
    assert resp.status_code == 401


def test_insufficient_role_rejected(client, db_session):
    plaintext, _ = _make_api_key(db_session, role="viewer", plaintext="rc_vieweronlykey0001")
    resp = client.post(
        "/api/v1/incidents",
        json={"title": "t", "service": "svc"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 403


def test_sufficient_role_api_key_allowed(client, db_session):
    plaintext, _ = _make_api_key(db_session, role="engineer", plaintext="rc_enginkeyabcdef0001")
    resp = client.post(
        "/api/v1/incidents",
        json={"title": "t", "service": "svc"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "t"


def test_admin_only_endpoint_rejects_engineer(client, db_session):
    plaintext, _ = _make_api_key(db_session, role="engineer", plaintext="rc_enginkeyabcdef0002")
    resp = client.get("/api/v1/audit-log", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 403


def test_admin_only_endpoint_allows_admin(client, db_session):
    plaintext, _ = _make_api_key(db_session, role="admin", plaintext="rc_adminkeyabcdef0001")
    resp = client.get("/api/v1/audit-log", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200


@pytest.mark.parametrize("algorithm", ["HS256", "none"])
def test_non_rs256_token_rejected(monkeypatch, rsa_keypair, algorithm):
    _, jwks = rsa_keypair
    settings = _configure_oidc_settings(monkeypatch, jwks)
    token = jwt.encode({"sub": "attacker", "iss": settings.oidc_issuer_url,
                        "aud": settings.oidc_audience, "roles": ["admin"]},
                       "attacker-controlled-secret" if algorithm == "HS256" else None,
                       algorithm=algorithm, headers={"kid": "test-kid"})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        verify_oidc_token(token)
    assert exc.value.status_code == 401


@pytest.mark.parametrize("field,value", [("iss", "https://wrong.example"), ("nbf", 9999999999)])
def test_oidc_claim_validation(monkeypatch, rsa_keypair, field, value):
    private_pem, jwks = rsa_keypair
    settings = _configure_oidc_settings(monkeypatch, jwks)
    claims = {"sub": "user", "iss": settings.oidc_issuer_url, "aud": settings.oidc_audience,
              "roles": ["engineer"], "exp": int(time.time()) + 3600, field: value}
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        verify_oidc_token(_issue_token(private_pem, claims))
    assert exc.value.status_code == 401


def test_rs256_key_without_optional_metadata(monkeypatch, rsa_keypair):
    private_pem, jwks = rsa_keypair
    del jwks["keys"][0]["alg"]
    del jwks["keys"][0]["use"]
    settings = _configure_oidc_settings(monkeypatch, jwks)
    token = _issue_token(private_pem, {"sub": "user", "iss": settings.oidc_issuer_url,
        "aud": settings.oidc_audience, "roles": ["engineer"], "exp": int(time.time()) + 3600})
    assert verify_oidc_token(token).role == "engineer"


@pytest.mark.parametrize("metadata", [{"alg": "HS256"}, {"use": "enc"}, {"key_ops": ["encrypt"]}])
def test_incompatible_jwk_metadata_rejected(monkeypatch, rsa_keypair, metadata):
    private_pem, jwks = rsa_keypair
    jwks["keys"][0].update(metadata)
    settings = _configure_oidc_settings(monkeypatch, jwks)
    token = _issue_token(private_pem, {"sub": "user", "iss": settings.oidc_issuer_url,
        "aud": settings.oidc_audience, "roles": ["admin"], "exp": int(time.time()) + 3600})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        verify_oidc_token(token)
    assert exc.value.status_code == 401
