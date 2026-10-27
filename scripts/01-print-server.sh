#!/bin/bash
# Install CUPS + HPLIP and share printers on the local network.
# Run on the Pi as root:  sudo ./01-print-server.sh [admin-user]
set -euo pipefail
ADMIN_USER="${1:-${SUDO_USER:-pi}}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q cups hplip printer-driver-hpcups avahi-daemon sane-utils libsane-hpaio

# Let the admin user manage printers in the CUPS web UI (http://<pi>:631)
usermod -aG lpadmin "$ADMIN_USER"

# Share printers, allow remote admin from the LAN, enable the web UI
cupsctl --remote-admin --remote-any --share-printers WebInterface=yes

# cups.service is only pulled in by printer.target (i.e. when a USB printer is
# detected), so without this the server is down after a boot with the printer off.
systemctl add-wants multi-user.target cups.service
systemctl enable --now cups avahi-daemon

# Only needed to discover *other* network printers
systemctl disable --now cups-browsed 2>/dev/null || true

echo "Print server ready: http://$(hostname -I | awk '{print $1}'):631"
