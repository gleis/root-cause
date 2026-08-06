# Scenario: Disk full (bounded, safe emptyDir)

customer-api's Deployment already mounts a small (100Mi, size-limited)
`emptyDir` at `/scratch` (see `infra/k3d/sample-app/k8s/deployment.yaml`).
This scenario calls the app's own `/fill-disk` endpoint repeatedly until
that 100Mi volume is exhausted and writes start failing -- entirely
contained to the pod's ephemeral volume, never touches host disk.

## What `trigger.sh` does
Calls `POST /fill-disk` (in a loop, via a one-off curl Job hitting the
in-cluster Service) several times; each call writes ~25MB until the 100Mi
`sizeLimit` is hit, at which point the kubelet starts evicting/erroring
writes and the app logs `OSError`/`No space left on device`-style errors
returned in the JSON response body.

## What to watch
```bash
kubectl -n demo logs job/disk-fill-gen
kubectl -n demo exec deploy/customer-api -- df -h /scratch
kubectl -n demo describe pod <customer-api-pod>   # watch for Evicted / DiskPressure if sizeLimit is exceeded
```

## Recovery
`resolve.sh` calls `POST /reset-disk` (removes all files `/fill-disk` wrote)
and deletes the load Job. Because it's an `emptyDir`, a pod restart alone
would also fully reclaim the space.
