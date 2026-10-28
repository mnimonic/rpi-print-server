#!/bin/bash
# Install EVERYTHING on a fresh Raspberry Pi: system packages, printer, tuning,
# configuration and the application. Run on your computer (Linux, macOS, or Git Bash on
# Windows), not on the Pi. Safe to run again: each step only changes what is missing.
#
#   ./deploy/deploy-install.sh                     app from the working tree
#   ./deploy/deploy-install.sh dist/rpi-print-server-v1.4.0.tar.gz   app from a release package
#
# Settings (environment variables):
#   PI_HOST, PI_USER, PI_PASS   how to reach the Pi (see deploy/lib.sh). PI_USER also becomes
#                               the CUPS admin and may scan from the command line.
#   PAPER=Letter                paper size of the printer queue (default A4)
#   WIFI_PROFILE=wlan0-MyWifi   optional, last step: switch the Pi to Wi-Fi only (see
#                               install/05-wifi-only.sh). Only use it while the Pi is on Ethernet.
#
# Before this, flash Raspberry Pi OS Lite with Raspberry Pi Imager and enable SSH there
# (see README.md, "Setup"). Takes 20-40 minutes on a Pi 1, mostly apt. Reboot afterwards.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

PKG="${1:-}"
case "$PKG" in -*) sed -n '6,7p' "$0" >&2; exit 2 ;; esac
[ -n "$PKG" ] || PKG="$(build_app_package)"

# The Pi gets the same three folders as the repository: install/, config/ and app/
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -r install config "$STAGE/"
unpack_app_package "$PKG" "$STAGE/app"

echo "Installing everything on $PI_USER@$PI_HOST (app $(cat "$STAGE/app/VERSION"))"
upload_and_run "$STAGE" \
    "ADMIN_USER=$PI_USER PAPER=${PAPER:-A4} WIFI_PROFILE=${WIFI_PROFILE:-} bash install/install-all.sh"
