# customer-api demo workload. Build locally and import into k3d with:
#   docker build -t customer-api:demo infra/k3d/sample-app
#   k3d image import customer-api:demo -c rootcause-demo
FROM python:3.12-slim

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY app.py .

EXPOSE 8080
# --threads so /work's time.sleep() calls don't serialize all requests
# behind a single worker thread -- lets WORKER_CONCURRENCY actually mean
# something under load-test traffic.
CMD ["gunicorn", "-w", "2", "--threads", "16", "-b", "0.0.0.0:8080", "app:app"]
