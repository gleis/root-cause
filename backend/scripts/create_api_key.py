#!/usr/bin/env python3
"""
Admin CLI to mint a new API key for service-to-service auth (Alertmanager
webhook, CI, scripts). Prints the plaintext key exactly once -- it is never
stored anywhere, only its SHA-256 hash. If you lose it, revoke it (see
POST /api/v1/api-keys/{id}/revoke) and mint a new one.

Usage:
    cd backend
    python scripts/create_api_key.py --role engineer --description "Alertmanager webhook"

Requires DATABASE_URL to point at the real database (this writes directly
to the api_keys table, bypassing the API).
"""
import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import hash_api_key, ROLE_ORDER  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app import models  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=ROLE_ORDER, default="viewer", help="Role granted to this key")
    parser.add_argument("--description", default="", help="Free-text note on what/who this key is for")
    args = parser.parse_args()

    # Generate a high-entropy plaintext key. Prefix identifies the app so
    # it's recognizable in logs/secret scanners without revealing anything
    # secret; the random part is what actually matters for security.
    plaintext = "rc_" + secrets.token_urlsafe(32)
    key_hash = hash_api_key(plaintext)
    prefix = plaintext[:11]  # "rc_" + first 8 chars of the random part

    init_db()  # no-op in real Postgres+Alembic setups; creates tables for a throwaway sqlite/demo DB
    db = SessionLocal()
    try:
        row = models.ApiKey(
            key_hash=key_hash,
            prefix=prefix,
            role=args.role,
            description=args.description,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    finally:
        db.close()

    print("=" * 70)
    print("API key created. This plaintext value is shown ONCE -- store it")
    print("in your secrets manager (e.g. Vault) now. It cannot be retrieved again.")
    print("=" * 70)
    print(f"  id:          {row.id}")
    print(f"  role:        {row.role}")
    print(f"  prefix:      {row.prefix}")
    print(f"  plaintext:   {plaintext}")
    print()
    print("Use it as either:")
    print(f"  Authorization: Bearer {plaintext}")
    print(f"  X-API-Key: {plaintext}")


if __name__ == "__main__":
    main()
