# Scenario: Broken deploy (CrashLoopBackOff)

Simulates a bad release: `trigger.sh` patches the `customer-api` Deployment
to use a nonexistent image tag AND a bad container command, so however the
image resolves, the container fails to start and Kubernetes puts it into
`CrashLoopBackOff`.

## What `trigger.sh` does
- Sets the container command to `["/bin/sh", "-c", "exit 1"]` (guaranteed
  crash on start regardless of image availability -- more reliable for a
  demo than relying on image-pull failures, which behave differently across
  registries/caches).
- Records the previous image/command so `resolve.sh` can restore them
  exactly (also just does `kubectl rollout undo`, which works here since
  it's a single bad revision).

## What to watch
```bash
kubectl -n demo get pods -w
kubectl -n demo describe pod <pod>   # Status: CrashLoopBackOff, rising restart count
kubectl -n demo logs <pod> --previous
```
GitLab-style "bad deploy" evidence in the AI Root Cause Investigator comes
from the ChangeAgent (real GitLab commit/MR/pipeline history around the
incident window) -- this scenario is primarily to exercise the
InfrastructureAgent/Kubernetes-events side (`Back-off restarting failed
container`, `CrashLoopBackOff` reason on pod events).

## Recovery
`resolve.sh` runs `kubectl rollout undo deployment/customer-api`, which
reverts to the previous (working) ReplicaSet.
