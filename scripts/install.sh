#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/wifi-fallback-relay}"
ENV_FILE="${ENV_FILE:-/etc/wifi-fallback-relay/config.env}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python binary not found: ${PYTHON_BIN}" >&2
  exit 1
fi

if ! command -v nmap >/dev/null 2>&1; then
  echo "nmap is required but not installed." >&2
  echo "Install with: sudo apt-get update; sudo apt-get install -y nmap" >&2
  exit 1
fi

echo "Installing wifi-fallback-relay to ${INSTALL_DIR}"
sudo mkdir -p "${INSTALL_DIR}"
if command -v rsync >/dev/null 2>&1; then
  sudo rsync -a --delete \
    --exclude ".venv" \
    --exclude "__pycache__" \
    --exclude "*.pyc" \
    "${PROJECT_DIR}/" "${INSTALL_DIR}/"
else
  echo "rsync not found; using cp fallback"
  sudo find "${INSTALL_DIR}" -mindepth 1 -maxdepth 1 ! -name ".venv" -exec rm -rf {} +
  sudo cp -a "${PROJECT_DIR}/." "${INSTALL_DIR}/"
fi

echo "Creating virtual environment"
sudo "${PYTHON_BIN}" -m venv "${INSTALL_DIR}/.venv"
sudo "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
sudo "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
sudo chmod +x "${INSTALL_DIR}/scripts/install.sh" "${INSTALL_DIR}/scripts/start-services.sh"

echo "Preparing environment file at ${ENV_FILE}"
sudo mkdir -p "$(dirname "${ENV_FILE}")"
if [[ ! -f "${ENV_FILE}" ]]; then
  sudo cp "${INSTALL_DIR}/.env.example" "${ENV_FILE}"
  echo "Created ${ENV_FILE}; edit it before starting services"
fi

echo "Installing systemd units"
sudo install -m 644 "${INSTALL_DIR}/systemd/rpi-master-relay.service" /etc/systemd/system/rpi-master-relay.service
sudo install -m 644 "${INSTALL_DIR}/systemd/rpi-slave-fallback.service" /etc/systemd/system/rpi-slave-fallback.service
sudo systemctl daemon-reload
sudo systemctl enable rpi-master-relay.service rpi-slave-fallback.service

echo "Installation complete"
echo "Next: edit ${ENV_FILE} and run scripts/start-services.sh <master|slave|all>"
