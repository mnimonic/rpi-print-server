#!/bin/bash
# Installs the application on the Pi: the welcome page (port 80) and the scan page (port
# 8080), each as a systemd service. deploy/deploy-app.sh puts this file in the release
# package and runs it there; by hand on the Pi:
#   tar -xzf rpi-print-server-v1.4.0.tar.gz && sudo ./rpi-print-server-v1.4.0/install.sh [--dry-run]
#   sudo ./app/install.sh [--dry-run]      from a copy of the repository
#
#   File in app/                Installed as
#   welcome/welcome.py          /usr/local/bin/welcome.py (with the version stamped in)
#   welcome/welcome.service     /etc/systemd/system/welcome.service
#   scanweb/scanweb.py          /usr/local/bin/scanweb.py
#   scanweb/scanweb.service     /etc/systemd/system/scanweb.service
#
# Works for a first install and for updates: only files that differ from the installed
# ones are copied, only the affected services are restarted, and then it checks that they
# are running (and shows their log if not).
set -euo pipefail
cd "$(dirname "$0")"
DRY_RUN=0
case "${1:-}" in
    "") ;;
    -n|--dry-run) DRY_RUN=1 ;;
    *) echo "Usage: $0 [--dry-run]" >&2; exit 2 ;;
esac
find . -type f -exec sed -i 's/\r$//' {} +   # in case the package was built from a CRLF checkout

# The version shown in the welcome page footer: from the package, else from git, else "dev"
if [ -f VERSION ]; then
    VERSION="$(cat VERSION)"
else
    VERSION="$(git -c safe.directory='*' describe --tags --always --dirty 2>/dev/null || echo dev)"
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
sed "s/^VERSION = \"dev\"/VERSION = \"$VERSION\"/" welcome/welcome.py > "$TMP/welcome.py"
grep -q "^VERSION = \"$VERSION\"$" "$TMP/welcome.py" || { echo "Could not stamp the version into welcome.py" >&2; exit 1; }

RESTART=()
RELOAD=0
put() {   # put <new file> <destination> <mode> <service to restart>
    local src=$1 dest=$2 mode=$3 svc=$4
    if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
        echo "  unchanged  $dest"
        return 0
    fi
    echo "  update     $dest"
    RESTART+=("$svc")
    if [ "$DRY_RUN" = 1 ]; then return 0; fi
    install -D -m "$mode" "$src" "$dest"
    if [[ $dest == /etc/systemd/system/* ]]; then RELOAD=1; fi
}

echo "Installing $VERSION on $(hostname):"

# The scan page runs as its own low-privilege user that may only use the scanner
if ! id scanweb >/dev/null 2>&1; then
    echo "  create     user scanweb (groups lp, scanner)"
    [ "$DRY_RUN" = 1 ] || useradd --system --no-create-home --shell /usr/sbin/nologin scanweb
fi
[ "$DRY_RUN" = 1 ] || usermod -aG lp,scanner scanweb

put "$TMP/welcome.py"       /usr/local/bin/welcome.py           755 welcome
put welcome/welcome.service /etc/systemd/system/welcome.service 644 welcome
put scanweb/scanweb.py      /usr/local/bin/scanweb.py           755 scanweb
put scanweb/scanweb.service /etc/systemd/system/scanweb.service 644 scanweb

if [ "$DRY_RUN" = 1 ]; then
    echo "Dry run: nothing was changed."
    exit 0
fi
if [ "$RELOAD" = 1 ]; then systemctl daemon-reload; fi
for s in welcome scanweb; do
    # Start at boot. A service that isn't running (e.g. after a failed start) is restarted too.
    systemctl is-enabled --quiet "$s" || systemctl enable --quiet "$s"
    systemctl is-active --quiet "$s" || RESTART+=("$s")
done
if [ ${#RESTART[@]} -eq 0 ]; then
    echo "Nothing to restart."
    exit 0
fi
SERVICES=$(printf '%s\n' "${RESTART[@]}" | sort -u)
for s in $SERVICES; do
    echo "Restarting $s"
    systemctl restart "$s"
done
sleep 4   # Pi 1: give Python a moment to start
FAILED=0
for s in $SERVICES; do
    if systemctl is-active --quiet "$s"; then
        echo "  $s is running"
    else
        echo "  $s FAILED to start:"
        journalctl -u "$s" -n 15 --no-pager
        FAILED=1
    fi
done
exit $FAILED
