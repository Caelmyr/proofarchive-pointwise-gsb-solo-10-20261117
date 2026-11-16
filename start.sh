#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
DOCKER_BUILDKIT=0 docker build -t solo13-proofarchive:1.0 .
docker compose up -d --no-build
