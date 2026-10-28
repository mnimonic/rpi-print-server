#!/bin/bash
# Deploy the CONFIGURATION files (config/) to the Pi: boot settings (config.txt), the
# HDMI-off service, the journal size limit and the Bonjour records.
# Run on your computer (Linux, macOS, or Git Bash on Windows), not on the Pi.
#
#   ./deploy/deploy-config.sh             install the config files from the working tree
#   ./deploy/deploy-config.sh --dry-run   only show which files would change
#
# Set PI_HOST, PI_USER and PI_PASS first (see deploy/lib.sh). Configuration isn't versioned
# like the application: whatever is in config/ right now is what gets installed. If the boot
# settings change, the script tells you to reboot the Pi.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

DRY_RUN=""
case "${1:-}" in
    "") ;;
    -n|--dry-run) DRY_RUN="--dry-run" ;;
    *) sed -n '6,7p' "$0" >&2; exit 2 ;;
esac

echo "Deploying config/ to $PI_USER@$PI_HOST"
upload_and_run config "bash install.sh $DRY_RUN"
