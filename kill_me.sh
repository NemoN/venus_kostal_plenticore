#!/bin/sh

SERVICE_DIR="/service/venus_kostal_plenticore"
PATTERN="/data/venus_kostal_plenticore/kostal.py"

# Stop the daemontools service first, otherwise supervise restarts the process.
if [ -d "$SERVICE_DIR" ] && command -v svc >/dev/null 2>&1; then
	svc -d "$SERVICE_DIR"
	sleep 1
fi

# Fallback: terminate any still running process.
pkill -TERM -f "$PATTERN" 2>/dev/null || true

