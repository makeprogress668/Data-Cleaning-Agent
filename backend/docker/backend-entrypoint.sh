#!/usr/bin/env sh
# Backend container entrypoint: start uvicorn with a configurable worker count.
#
# File/thread mode requires one worker. PostgreSQL + ARQ mode may run multiple API
# workers because execution happens in dedicated durable workers and status claims
# use database CAS. The application validates that combination at startup.
set -eu

WORKERS="${DATA_AGENT_API_WORKERS:-1}"
HOST="${DATA_AGENT_API_HOST:-0.0.0.0}"
PORT="${DATA_AGENT_API_PORT:-8000}"
FORWARDED_ALLOW_IPS="${DATA_AGENT_FORWARDED_ALLOW_IPS:-127.0.0.1}"

exec uvicorn data_agent.api:app \
  --host "${HOST}" \
  --port "${PORT}" \
  --workers "${WORKERS}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS}"
