#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# The host's build proxy can be on loopback. Application containers use their own network.
build_args=(--network=host --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY)
docker build "${build_args[@]}" -f Dockerfile.local -t harei-local-backend .
docker build "${build_args[@]}" --target local -f ../harei-frontend/Dockerfile.local -t harei-local-frontend ../harei-frontend
docker compose --env-file /dev/null -f compose.local.yaml up -d --wait
