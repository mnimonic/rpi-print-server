#!/bin/bash
# Make the server easy to find on the LAN:
#  - Bonjour/mDNS records for the web pages (Avahi; <hostname>.local already works)
#  - SSDP/UPnP and the QR-code info sheet are built into welcome.py (reinstalled here)
# Run on the Pi as root:  sudo ./08-advertise.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"

# Avahi picks up new files in /etc/avahi/services/ by itself
install -m 644 "$HERE"/config/avahi/printserver-*.service /etc/avahi/services/

install -m 755 "$HERE/welcome/welcome.py" /usr/local/bin/welcome.py
# Show the git version on the page (e.g. v1.0.0); stays "dev" outside a git checkout
VERSION="$(git -c safe.directory="$HERE" -C "$HERE" describe --tags --always --dirty 2>/dev/null || echo dev)"
sed -i "s/^VERSION = \"dev\"/VERSION = \"$VERSION\"/" /usr/local/bin/welcome.py
install -m 644 "$HERE/welcome/welcome.service" /etc/systemd/system/welcome.service
systemctl daemon-reload
systemctl restart welcome.service
sleep 3
systemctl is-active welcome.service avahi-daemon
IP="$(hostname -I | awk '{print $1}')"
echo "Welcome page: http://$IP/  or  http://$(hostname).local/"
echo "Info sheet:   http://$IP/sheet"
