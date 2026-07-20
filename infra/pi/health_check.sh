#!/bin/bash
# Deep health check for keepalived track_script.
# Exit 0 only when FastAPI and InfluxDB both respond 200.
set -euo pipefail

FASTAPI_OK=$(curl -sf -o /dev/null -w "%{http_code}" --max-time 2 http://127.0.0.1:8000/health || echo "000")
INFLUX_OK=$(curl -sf -o /dev/null -w "%{http_code}" --max-time 2 http://127.0.0.1:8086/health || echo "000")

if [ "$FASTAPI_OK" = "200" ] && [ "$INFLUX_OK" = "200" ]; then
    exit 0
fi
exit 1
