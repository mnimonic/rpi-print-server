#!/bin/bash
# Install the scan web page (http://<pi>:8080) as a systemd service.
# Run on the Pi as root:  sudo ./06-install-scanweb.sh [admin-user]
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ADMIN_USER="${1:-${SUDO_USER:-pi}}"

# Dedicated low-privilege user that may only use the scanner
id scanweb >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin scanweb
usermod -aG lp,scanner scanweb
# Let the admin user scan from the command line too (scanimage)
usermod -aG lp,scanner "$ADMIN_USER"

install -m 755 "$HERE/scanweb/scanweb.py" /usr/local/bin/scanweb.py
install -m 644 "$HERE/scanweb/scanweb.service" /etc/systemd/system/scanweb.service
systemctl daemon-reload
systemctl enable --now scanweb.service
sleep 3
systemctl is-active scanweb.service
echo "Scan page: http://$(hostname -I | awk '{print $1}'):8080"
