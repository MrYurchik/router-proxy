#!/bin/bash
# run_proxy.sh — Export env vars and start the routing proxy

export ROUTING_CONFIG="${ROUTING_CONFIG:-/home/yurii/hermes-routing.yaml}"
export PROXY_STATE_DB="${PROXY_STATE_DB:-/home/yurii/proxy_state.db}"

# Source API keys from Hermes .env
set -a
source /home/yurii/.hermes/.env 2>/dev/null
set +a

cd /home/yurii/router_proxy
source .venv/bin/activate
export PYTHONPATH=/home/yurii

exec uvicorn router_proxy.app:app --host 0.0.0.0 --port "${PORT:-8787}"
