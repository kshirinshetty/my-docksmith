#!/bin/sh
# Sample Docksmith container application
# Demonstrates ENV injection, WORKDIR, CMD, and filesystem isolation.

echo "================================================="
echo " ${APP_NAME:-DocksmithApp} v${APP_VERSION:-?}"
echo "================================================="
echo ""
echo "Container environment:"
echo "  APP_NAME    = ${APP_NAME}"
echo "  APP_VERSION = ${APP_VERSION}"
echo "  WORKDIR     = $(pwd)"
echo ""
echo "Build info:"
if [ -f /app/output/info.txt ]; then
    cat /app/output/info.txt
else
    echo "  (no build info found)"
fi
echo ""
echo "Files in /app:"
ls /app/
echo ""
echo "--- Isolation test ---"
echo "Writing /tmp/isolation_test.txt inside container..."
echo "created inside docksmith container at $(date)" > /tmp/isolation_test.txt
echo "Done. This file lives at /tmp/isolation_test.txt inside the container."
echo "It must NOT exist at /tmp/isolation_test.txt on the host."
echo ""
echo "Container exiting cleanly."
