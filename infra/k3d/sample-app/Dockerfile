# customer-api demo workload. Build locally and import into k3d with:
#   docker build -t customer-api:demo infra/k3d/sample-app
#   k3d image import customer-api:demo -c rootcause-demo
FROM cgr.dev/chainguard/wolfi-base@sha256:8509ee4351a2e501b7f98201f2e2642396e82020c61a589cacdcfe74b9de46c1

RUN apk add --no-cache python-3.12=3.12.16_git20261030-r0 py3.12-pip=26.2.2-r2 \
    && python3.12 -m venv /opt/venv
ENV PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock \
    && pip check
COPY app.py .

EXPOSE 8080
# --threads so /work's time.sleep() calls don't serialize all requests
# behind a single worker thread -- lets WORKER_CONCURRENCY actually mean
# something under load-test traffic.
CMD ["gunicorn", "-w", "2", "--threads", "16", "-b", "0.0.0.0:8080", "app:app"]
