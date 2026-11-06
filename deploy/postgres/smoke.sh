#!/usr/bin/env bash
set -euo pipefail
image="${1:-rootcause-postgres:16-secure}"
name="rootcause-postgres-check-$$"
volume="$name"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; docker volume rm "$volume" >/dev/null 2>&1 || true; }
trap cleanup EXIT
[[ $(docker run --rm --entrypoint gosu "$image" postgres id -u) == 70 ]]
[[ $(docker run --rm --entrypoint gosu "$image" 1234:1235 id -g) == 1235 ]]
[[ $(docker run --rm --entrypoint gosu "$image" postgres sh -c 'echo "$HOME"') == /var/lib/postgresql ]]
set +e
docker run --rm --entrypoint gosu "$image" postgres sh -c 'exit 42'
status=$?
set -e
[[ $status == 42 ]]
docker volume create "$volume" >/dev/null
start() {
  docker run -d --name "$name" -e POSTGRES_PASSWORD=smoke -e POSTGRES_USER=demo -e POSTGRES_DB=demo -e PGDATA=/var/lib/postgresql/data/pgdata -v "$volume:/var/lib/postgresql/data" "$image" -c max_connections=15 >/dev/null
  for _ in $(seq 1 60); do
    if docker exec "$name" pg_isready -U demo >/dev/null 2>&1; then return; fi
    sleep 1
  done
  docker logs "$name"; return 1
}
start
docker exec "$name" psql -U demo -d demo -v ON_ERROR_STOP=1 -c 'CREATE TABLE maintenance_sentinel (id int PRIMARY KEY); INSERT INTO maintenance_sentinel VALUES (42);' >/dev/null
[[ $(docker exec "$name" stat -c %u /proc/1) == 70 ]]
docker rm -f "$name" >/dev/null
start
[[ $(docker exec "$name" psql -U demo -d demo -Atc 'SELECT id FROM maintenance_sentinel') == 42 ]]
[[ $(docker exec "$name" psql -U demo -d demo -Atc 'SHOW max_connections') == 15 ]]
echo 'PostgreSQL privilege drop, fresh initialization, configuration and persistence passed'
