# Scenario: DNS misconfiguration

Points customer-api's downstream DB host at a nonexistent service name, so
every "downstream" DNS lookup fails. (We patch the app's own env rather than
CoreDNS's ConfigMap -- editing CoreDNS cluster-wide risks breaking the
demo's other components including Prometheus/Loki scraping; patching a
single Deployment's env is safer and just as demonstrative of "DNS
resolution failure" symptoms in logs.)

## What `trigger.sh` does
Sets `DOWNSTREAM_DB_HOST=postgres-demo.nonexistent-namespace.svc.cluster.local`
on the `customer-api` Deployment (triggers a rolling restart) and launches a
small load-generator Job that repeatedly curls `/work` — traffic itself
keeps flowing (this app doesn't actually depend on `DOWNSTREAM_DB_HOST` for
`/work`), so to actually produce DNS-failure evidence we run a sidecar-style
one-off pod that does `nslookup`/`wget` against the bad hostname in a loop
and logs the resolution failures, standing in for what a real app's error
logs would show ("could not translate host name ... Name or service not
known").

## What to watch
```bash
kubectl -n demo logs job/dns-failure-gen
```
You'll see repeated `bad address 'postgres-demo.nonexistent-namespace...'`
/ `NXDOMAIN` lines -- this is the log signal the LogAgent would pick up via
Loki/Promtail in the full pipeline.

## Recovery
`resolve.sh` deletes the load-generator Job and reverts
`DOWNSTREAM_DB_HOST` to `postgres-demo` (the ConfigMap default).
