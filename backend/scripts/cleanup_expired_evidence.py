#!/usr/bin/env python3
"""
Data retention: deletes Evidence rows older than EVIDENCE_RETENTION_DAYS
(default 90; see Settings.evidence_retention_days / .env.example).

Meant to run as a scheduled job (cron or a Kubernetes CronJob -- see
docs/deployment.md), NOT as part of the API request path.

Only Evidence rows are pruned, not Incidents/Hypotheses/postmortems --
postmortems are the durable artifact meant to be kept long-term; raw
Evidence (metric snapshots, log excerpts, etc) is the bulky, less valuable
thing that should age out. If Evidence.raw_reference ever stores a pointer
to an out-of-DB blob (e.g. a local file path or object-storage key) rather
than inline JSON, this script also removes that blob -- see
_delete_raw_blob_if_local() below.

Usage:
    cd backend
    python scripts/cleanup_expired_evidence.py                  # deletes rows past retention
    python scripts/cleanup_expired_evidence.py --dry-run         # reports what WOULD be deleted
    python scripts/cleanup_expired_evidence.py --retention-days 30
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app import models  # noqa: E402


def _delete_raw_blob_if_local(raw_reference: dict) -> None:
    """If raw_reference points at a locally-stored file (e.g.
    {"local_path": "/var/lib/rootcause/evidence/<id>.json"}), remove it.
    No-op for the common case where raw_reference is fully inline JSON
    (which is what every current integration client produces) or points at
    a remote system (Prometheus/Loki/GitLab query, not a local file) that
    this script has no business deleting from."""
    path = (raw_reference or {}).get("local_path")
    if path and os.path.isfile(path):
        os.remove(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted, delete nothing")
    parser.add_argument("--retention-days", type=int, default=None, help="Override EVIDENCE_RETENTION_DAYS")
    args = parser.parse_args()

    settings = get_settings()
    retention_days = args.retention_days if args.retention_days is not None else settings.evidence_retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    db = SessionLocal()
    try:
        rows = db.query(models.Evidence).filter(models.Evidence.timestamp < cutoff).all()
        print(f"Retention window: {retention_days} days (cutoff={cutoff.isoformat()})")
        print(f"Found {len(rows)} Evidence row(s) past retention.")

        if not rows:
            return

        if args.dry_run:
            print("--dry-run: not deleting. Sample of what would be removed:")
            for r in rows[:20]:
                print(f"  evidence_id={r.evidence_id} incident_id={r.incident_id} timestamp={r.timestamp.isoformat()} source={r.source}")
            if len(rows) > 20:
                print(f"  ... and {len(rows) - 20} more")
            return

        deleted = 0
        for r in rows:
            _delete_raw_blob_if_local(r.raw_reference or {})
            db.delete(r)
            deleted += 1
        db.commit()
        print(f"Deleted {deleted} Evidence row(s) (and any associated local blobs).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
