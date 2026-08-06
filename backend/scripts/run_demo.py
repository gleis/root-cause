#!/usr/bin/env python3
"""
Single-command, end-to-end demo of the AI Root Cause Investigator with NO
live infrastructure required.

Usage:
    cd backend
    DEMO_MODE=true python scripts/run_demo.py

What it does:
  1. Forces DEMO_MODE=true and points the DB at a throwaway local SQLite file
     (so no Postgres/Docker is required just to see the pipeline run -- the
     real deployment uses Postgres via DATABASE_URL, see .env.example).
  2. Creates a sample incident directly against the data layer (equivalent
     to POST /api/v1/incidents).
  3. Runs the full agent pipeline (Coordinator -> Metrics/Log/Change/Infra/
     Dependency -> Hypothesis -> Reporting) against the canned OOMKilled
     fixtures in app/integrations/fixtures/.
  4. Prints the timeline, ranked hypotheses (with evidence citations and
     confidence), and the Slack summary text to stdout.
  5. Writes the generated postmortem markdown to ./demo_postmortem.md.

If a local Ollama server with a pulled model (default: qwen2.5) is NOT
running, the pipeline still completes -- HypothesisAgent and ReportingAgent
fall back to deterministic templated output (clearly marked as such) instead
of failing, so this script always produces a complete result.
"""
import asyncio
import os
import sys
import tempfile

# Force demo-friendly settings BEFORE importing the app package, since
# config.Settings() reads the environment at import time.
os.environ.setdefault("DEMO_MODE", "true")
# Use a system temp dir rather than the current directory for the throwaway
# SQLite file -- some sandboxed / networked filesystems (e.g. certain
# container bind-mounts) don't support SQLite's file-locking reliably, which
# surfaces as "disk I/O error" if the DB lives on one of those. /tmp is
# always a local, real filesystem.
_DEMO_DB_PATH = os.path.join(tempfile.gettempdir(), "rootcause_demo.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DEMO_DB_PATH}")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: E402
from app.agents.coordinator import run_investigation  # noqa: E402


def main():
    print("=" * 78)
    print("AI Root Cause Investigator -- DEMO MODE (no live infrastructure)")
    print("=" * 78)

    # Fresh schema each run for a clean, reproducible demo.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        incident_start = datetime.now(timezone.utc) - timedelta(minutes=40)
        incident = models.Incident(
            title="Checkout API elevated error rate / pod restarts",
            severity="sev1",
            environment="production",
            service="checkout-api",
            started_at=incident_start,
            affected_components=["checkout-prod"],
            status="investigating",
        )
        db.add(incident)
        db.commit()
        db.refresh(incident)
        print(f"\nCreated incident {incident.incident_id} (service={incident.service}, started_at={incident.started_at.isoformat()})\n")

        incident = asyncio.run(run_investigation(incident.incident_id, db))

        # ---- Timeline ----
        timeline_rows = (
            db.query(models.Evidence)
            .filter_by(incident_id=incident.incident_id)
            .order_by(models.Evidence.timestamp.asc())
            .all()
        )
        print("-" * 78)
        print(f"TIMELINE ({len(timeline_rows)} events)")
        print("-" * 78)
        for e in timeline_rows:
            print(f"  {e.timestamp.isoformat()}  [{e.source}/{e.type}]  {e.summary}")

        # ---- Hypotheses ----
        hyps = (
            db.query(models.Hypothesis)
            .filter_by(incident_id=incident.incident_id)
            .order_by(models.Hypothesis.confidence.desc())
            .all()
        )
        print("\n" + "-" * 78)
        print(f"RANKED HYPOTHESES ({len(hyps)})")
        print("-" * 78)
        for i, h in enumerate(hyps, 1):
            print(f"\n{i}. [{h.confidence*100:.0f}% confidence] {h.statement}")
            print(f"   status: {h.status}")
            print(f"   rationale: {h.rationale}")
            print(f"   supporting evidence: {h.supporting_evidence}")
            print(f"   contradicting evidence: {h.contradicting_evidence}")
            if h.score_breakdown:
                print(f"   score breakdown: {h.score_breakdown}")

        # ---- Slack summary ----
        print("\n" + "-" * 78)
        print("SLACK SUMMARY")
        print("-" * 78)
        print(incident.slack_summary or "(none generated)")

        # ---- Postmortem ----
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "demo_postmortem.md")
        out_path = os.path.abspath(out_path)
        with open(out_path, "w") as f:
            f.write(incident.postmortem_markdown or "(no postmortem generated)")
        print("\n" + "-" * 78)
        print(f"Postmortem written to: {out_path}")
        print("-" * 78)

    finally:
        db.close()


if __name__ == "__main__":
    main()
