#!/bin/bash
# Installs an unpacked release package on the Pi. scripts/release.sh packages this file as
# install.sh next to the files; scripts/deploy.sh uploads the package and runs it.
# By hand on the Pi:
#   tar -xzf rpi-print-server-v1.2.0.tar.gz && sudo ./rpi-print-server-v1.2.0/install.sh [--dry-run]
#
# Only files that differ from the installed ones are copied, only the affected services are
# restarted, and components that were never set up (scripts 01-07) are skipped.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f VERSION ]; then
    echo "Run this from an unpacked release package (scripts/release.sh builds one)." >&2
    exit 1
fi
DRY_RUN=0
case "${1:-}" in
    "") ;;
    -n|--dry-run) DRY_RUN=1 ;;
    *) echo "Usage: $0 [--dry-run]" >&2; exit 2 ;;
esac
find . -type f -exec sed -i 's/\r$//' {} +   # in case the package was built from a CRLF checkout

RESTART=()
RELOAD=0
put() {   # put <package file> <destination> <mode> [service to restart]
    local src=$1 dest=$2 mode=$3 svc=${4:-}
    if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
        echo "  unchanged  $dest"
        return 0
    fi
    echo "  update     $dest"
    if [ "$DRY_RUN" = 1 ]; then return 0; fi
    install -D -m "$mode" "$src" "$dest"
    if [[ $dest == /etc/systemd/system/* ]]; then RELOAD=1; fi
    if [ -n "$svc" ]; then RESTART+=("$svc"); fi
}
installed() {   # installed <service> <setup script>
    if [ -f "/etc/systemd/system/$1.service" ]; then return 0; fi
    echo "  skipped    $1 (not installed; run $2 on the Pi first)"
    return 1
}

echo "Installing $(cat VERSION) on $(hostname):"
if installed welcome scripts/07-install-welcome.sh; then
    put welcome/welcome.py /usr/local/bin/welcome.py 755 welcome
    put welcome/welcome.service /etc/systemd/system/welcome.service 644 welcome
fi
if installed scanweb scripts/06-install-scanweb.sh; then
    put scanweb/scanweb.py /usr/local/bin/scanweb.py 755 scanweb
    put scanweb/scanweb.service /etc/systemd/system/scanweb.service 644 scanweb
fi
if installed hdmi-off scripts/03-tune-headless.sh; then
    put config/hdmi-off.service /etc/systemd/system/hdmi-off.service 644   # takes effect at boot
fi
if [ -d /etc/avahi/services ]; then
    # Avahi notices new or changed files by itself
    put config/avahi/printserver-web.service /etc/avahi/services/printserver-web.service 644
    put config/avahi/printserver-scan.service /etc/avahi/services/printserver-scan.service 644
else
    echo "  skipped    Avahi records (avahi-daemon not installed; run scripts/01-print-server.sh)"
fi

if [ "$DRY_RUN" = 1 ]; then
    echo "Dry run: nothing was changed."
    exit 0
fi
if [ "$RELOAD" = 1 ]; then systemctl daemon-reload; fi
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
