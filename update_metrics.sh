#!/bin/bash
# Update KGX Storage metrics
# Run this after a pipeline build has written to the bucket. The folder statistics only
# change then, so a run on a clock rescans data that has not moved.

cd /home/ubuntu/kgx-storage-webserver

METRICS_LOG=/var/log/kgx-storage/metrics.log
VENV_PY=/home/ubuntu/kgx-storage-webserver/.venv/bin/python

# Compute new metrics: always append to log; mirror to terminal when run interactively
if [ -t 1 ]; then
  set -o pipefail
  "$VENV_PY" -u compute_metrics.py 2>&1 | tee -a "$METRICS_LOG"
else
  "$VENV_PY" -u compute_metrics.py >> "$METRICS_LOG" 2>&1
fi

# Reload gunicorn workers gracefully (HUP signal). Kept for older deployments only:
# the web server now rereads metrics.json whenever its mtime changes.
pkill -HUP -f "gunicorn.*web_server:app" 2>&1 | logger -t kgx-metrics

if [ -t 1 ]; then
  echo "[$(date)] Metrics updated and workers reloaded" | tee -a "$METRICS_LOG"
else
  echo "[$(date)] Metrics updated and workers reloaded" >> "$METRICS_LOG"
fi
