# Scenario: Expired / self-signed TLS certificate

Deploys a small nginx TLS sidecar-Service (`customer-api-tls`) in front of
customer-api using a certificate that's already expired (generated with
`openssl` using `-startdate` / `-enddate` in the past), so TLS handshakes
against it fail with a certificate-expiry error -- mirroring a real
"cert didn't get rotated" incident.

## What `trigger.sh` does
1. Generates a self-signed cert/key with `openssl req -x509` backdated so
   `notAfter` is already in the past (`-enddate` set to yesterday).
2. Creates a `tls` Secret from it and an nginx Deployment+Service
   (`customer-api-tls`) that terminates TLS using that Secret and proxies
   to `customer-api:80`.
3. Runs a one-off Job that curls `https://customer-api-tls` (with cert
   validation ON, i.e. no `-k`) in a loop, logging the handshake failures.

## What to watch
```bash
kubectl -n demo logs job/tls-handshake-fail
# or manually:
kubectl -n demo run tls-check --rm -it --image=curlimages/curl --restart=Never -- \
  curl -v https://customer-api-tls
```
Expect `SSL certificate problem: certificate has expired`.

## Recovery
`resolve.sh` deletes the Job, the nginx Deployment/Service, and the TLS
Secret entirely (cert material isn't reused across runs -- `trigger.sh`
regenerates it fresh each time).
