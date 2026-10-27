#!/bin/bash
# Install the welcome page (http://<pi>/) as a systemd service.
# Run on the Pi as root:  sudo ./07-install-welcome.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"

install -m 755 "$HERE/welcome/welcome.py" /usr/local/bin/welcome.py
# Show the git version on the page (e.g. v1.0.0); stays "dev" outside a git checkout
VERSION="$(git -c safe.directory="$HERE" -C "$HERE" describe --tags --always --dirty 2>/dev/null || echo dev)"
sed -i "s/^VERSION = \"dev\"/VERSION = \"$VERSION\"/" /usr/local/bin/welcome.py
install -m 644 "$HERE/welcome/welcome.service" /etc/systemd/system/welcome.service
systemctl daemon-reload
systemctl enable --now welcome.service
sleep 3
systemctl is-active welcome.service
echo "Welcome page: http://$(hostname -I | awk '{print $1}')/"
