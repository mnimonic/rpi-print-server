#!/bin/bash
# Runs the whole first-time setup on the Pi, in order. deploy/deploy-install.sh uploads the
# repository's install/, config/ and app/ folders and runs this; you can also run it from a
# copy of the repository on the Pi:
#   sudo ./install/install-all.sh
#
# Settings (environment variables, all optional):
#   ADMIN_USER     the user who may manage printers and scan (default: the one running sudo)
#   PAPER          paper size of the printer queue (default A4)
#   WIFI_PROFILE   switch to Wi-Fi only at the end (see 05-wifi-only.sh)
#
# Every step can be run again safely.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo $0" >&2; exit 1; }
export ADMIN_USER="${ADMIN_USER:-${SUDO_USER:-pi}}"
export PAPER="${PAPER:-A4}"

step() { echo; echo "==== $*"; }

step "1/7 Packages: CUPS, HP drivers, SANE, Avahi"
bash "$HERE/01-print-server.sh" "$ADMIN_USER"

step "2/7 Printer queue"
bash "$HERE/02-add-printer.sh"

step "3/7 Headless tuning (disable unneeded services)"
bash "$HERE/03-tune-headless.sh"

step "4/7 NetworkManager without netplan (faster boot)"
bash "$HERE/04-networkmanager-without-netplan.sh"

step "5/7 Configuration files"
bash "$ROOT/config/install.sh"

step "6/7 Application (welcome page and scan page)"
bash "$ROOT/app/install.sh"

if [ -n "${WIFI_PROFILE:-}" ]; then
    step "7/7 Wi-Fi only"
    WIFI_PROFILE="$WIFI_PROFILE" bash "$HERE/05-wifi-only.sh"
    echo
    echo "Setup finished. The network is switching now: wait ~30 s, check that"
    echo "http://$(hostname).local/ opens, then reboot (sudo reboot)."
else
    step "7/7 Wi-Fi only: skipped (set WIFI_PROFILE to use it)"
    echo
    echo "Setup finished. Reboot to apply the boot settings: sudo reboot"
    echo "Then open http://$(hostname).local/"
fi
