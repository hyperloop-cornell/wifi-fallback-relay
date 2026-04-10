#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-all}"

case "${MODE}" in
  master)
    sudo systemctl restart rpi-master-relay.service
    sudo systemctl status --no-pager rpi-master-relay.service
    ;;
  slave)
    sudo systemctl restart rpi-slave-fallback.service
    sudo systemctl status --no-pager rpi-slave-fallback.service
    ;;
  all)
    sudo systemctl restart rpi-master-relay.service
    sudo systemctl restart rpi-slave-fallback.service
    sudo systemctl status --no-pager rpi-master-relay.service rpi-slave-fallback.service
    ;;
  *)
    echo "Usage: $0 [master|slave|all]" >&2
    exit 1
    ;;
esac
