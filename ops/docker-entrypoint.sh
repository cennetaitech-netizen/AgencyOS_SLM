#!/bin/sh
# Starts with an empty metrics directory, so counters from a previous run of this container aren't summed in
# (see chat_service/metrics.py: worker processes share their counters through these files).
set -e
if [ -n "$PROMETHEUS_MULTIPROC_DIR" ]; then
  rm -rf "$PROMETHEUS_MULTIPROC_DIR"
  mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
fi
exec "$@"
