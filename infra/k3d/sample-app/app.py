"""
customer-api: a minimal Flask app used as the demo workload for the
AI Root Cause Investigator's live k3d scenarios (infra/k3d/scenarios/).

It intentionally does very little "real" work -- its job is to produce
believable CPU / memory / error-rate / latency signal on Prometheus and
structured logs on stdout (scraped by Promtail -> Loki), and to be cheaply
reconfigurable (via env vars, changed with `kubectl set env` /
`kubectl patch`) so each failure scenario can be triggered and reversed
without redeploying a different image.

Env vars (all optional, sane defaults):
  WORKER_CONCURRENCY   int, default 10   -- simulated in-flight "workers";
                                            each in-flight request holds
                                            MEM_PER_WORKER_MB of memory for
                                            the OOMKilled scenario.
  MEM_PER_WORKER_MB    int, default 4    -- MB "leaked"/held per concurrent
                                            in-flight request while handling
                                            /work.
  BASE_MEM_MB          int, default 20   -- memory allocated once at
                                            startup, simulating baseline RSS.
  ERROR_RATE_PCT       float, default 0  -- % of /work requests that return
                                            HTTP 500.
  LATENCY_MS           int, default 20   -- artificial per-request latency
                                            (high-latency scenario).
  CACHE_ENABLED        "true"/"false", default "true" -- when false, /work
                                            simulates an extra "DB roundtrip"
                                            sleep, standing in for a disabled
                                            Redis cache in front of the DB.
  DOWNSTREAM_DB_HOST    hostname, default "postgres-demo" -- used only by
                                            the db-connection-exhaustion
                                            scenario's load generator, not
                                            required for the app to boot.
  DISK_SCRATCH_DIR      path, default "/scratch" -- where /fill-disk writes
                                            (disk-full scenario); mounted as
                                            a small emptyDir/PVC.

Endpoints:
  GET  /work        simulate a unit of customer-api work (the "real"
                     traffic path the scenarios generate load against)
  GET  /healthz      liveness
  GET  /readyz        readiness
  GET  /metrics      Prometheus exposition format
  POST /fill-disk     writes `bytes` (query param, default 10MB) of junk data
                       to DISK_SCRATCH_DIR, for the disk-full scenario
  POST /reset-disk     removes files written by /fill-disk
"""
import os
import random
import string
import time
import logging
from threading import Lock

from flask import Flask, request, jsonify, Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s customer-api %(message)s")
logger = logging.getLogger("customer-api")

app = Flask(__name__)

REQUESTS_TOTAL = Counter("http_requests_total", "Total HTTP requests", ["method", "path", "status"])
REQUEST_LATENCY = Histogram("http_request_duration_seconds", "Request latency", ["path"])
IN_FLIGHT = Gauge("customer_api_in_flight_requests", "Simulated in-flight worker count")
MEM_HELD_BYTES = Gauge("customer_api_simulated_memory_bytes", "Bytes intentionally held to simulate memory pressure")

_mem_lock = Lock()
_held_buffers = []  # list of bytearrays we intentionally keep alive
_disk_files = []


def _mb(n):
    return int(n) * 1024 * 1024


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# Baseline memory allocated once at import time so the process has a
# realistic non-zero starting RSS (baseline ~62% in the OOMKilled demo).
_baseline_buf = bytearray(_mb(_env_int("BASE_MEM_MB", 20)))


@app.route("/healthz")
def healthz():
    return jsonify(status="ok")


@app.route("/readyz")
def readyz():
    return jsonify(status="ready")


@app.route("/work")
def work():
    start = time.time()
    concurrency = _env_int("WORKER_CONCURRENCY", 10)
    mem_per_worker_mb = _env_int("MEM_PER_WORKER_MB", 4)
    error_rate_pct = _env_float("ERROR_RATE_PCT", 0)
    latency_ms = _env_int("LATENCY_MS", 20)
    cache_enabled = _env_bool("CACHE_ENABLED", True)

    IN_FLIGHT.inc()
    try:
        # Simulate each "concurrent worker slot" holding some memory. This is
        # deliberately crude (not real concurrency control) -- its purpose is
        # to make container_memory_working_set_bytes climb roughly with
        # WORKER_CONCURRENCY * MEM_PER_WORKER_MB so the OOMKilled scenario is
        # reproducible by just bumping WORKER_CONCURRENCY and/or lowering the
        # pod's memory limit.
        with _mem_lock:
            while len(_held_buffers) < concurrency:
                _held_buffers.append(bytearray(_mb(mem_per_worker_mb)))
            while len(_held_buffers) > concurrency:
                _held_buffers.pop()
            MEM_HELD_BYTES.set(sum(len(b) for b in _held_buffers) + len(_baseline_buf))

        # Disabled-cache scenario: no cache -> extra simulated DB roundtrip.
        if not cache_enabled:
            time.sleep(0.05)

        # High-latency scenario: artificial sleep.
        if latency_ms > 0:
            time.sleep(latency_ms / 1000.0)

        # Error-rate scenario / general error injection.
        if error_rate_pct > 0 and random.random() * 100 < error_rate_pct:
            REQUESTS_TOTAL.labels("GET", "/work", "500").inc()
            logger.error("simulated downstream failure handling /work (error_rate_pct=%s)", error_rate_pct)
            return jsonify(error="simulated failure"), 500

        payload = "".join(random.choices(string.ascii_letters, k=16))
        REQUESTS_TOTAL.labels("GET", "/work", "200").inc()
        logger.info("served /work ok payload=%s concurrency=%d", payload, concurrency)
        return jsonify(status="ok", payload=payload)
    finally:
        IN_FLIGHT.dec()
        REQUEST_LATENCY.labels("/work").observe(time.time() - start)


@app.route("/fill-disk", methods=["POST"])
def fill_disk():
    """Writes junk data to a small bounded scratch volume until it's full,
    for the disk-full scenario. Mount DISK_SCRATCH_DIR as a small (e.g.
    100Mi) emptyDir/PVC so this can never touch host disk."""
    scratch_dir = os.environ.get("DISK_SCRATCH_DIR", "/scratch")
    os.makedirs(scratch_dir, exist_ok=True)
    chunk_mb = _env_int("FILL_CHUNK_MB", 5)
    written_mb = 0
    errors = []
    try:
        for i in range(200):  # hard cap so this always terminates even on a huge volume
            path = os.path.join(scratch_dir, f"junk_{len(_disk_files)}_{i}.bin")
            try:
                with open(path, "wb") as f:
                    f.write(os.urandom(_mb(chunk_mb)))
                _disk_files.append(path)
                written_mb += chunk_mb
            except OSError as exc:
                errors.append(str(exc))
                break
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
    logger.error("fill-disk wrote %dMB to %s, errors=%s", written_mb, scratch_dir, errors)
    return jsonify(written_mb=written_mb, errors=errors)


@app.route("/reset-disk", methods=["POST"])
def reset_disk():
    removed = 0
    for path in list(_disk_files):
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
        _disk_files.remove(path)
    return jsonify(removed=removed)


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    port = _env_int("PORT", 8080)
    app.run(host="0.0.0.0", port=port)
