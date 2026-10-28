#!/bin/bash
# Installs the configuration files in this folder on the Pi. deploy/deploy-config.sh
# uploads the folder and runs this; by hand on the Pi:
#   sudo ./config/install.sh [--dry-run]
#
#   File in config/                    Installed as
#   config.txt.append                  end of /boot/firmware/config.txt (headless boot settings)
#   hdmi-off.service                   /etc/systemd/system/ (turns HDMI off at boot)
#   journald-size.conf                 /etc/systemd/journald.conf.d/size.conf (small log)
#   avahi/printserver-*.service        /etc/avahi/services/ (Bonjour records for both pages)
#   wifi.nmconnection.example          not installed: a template, see README.md
#
# Only files that differ from the installed ones are written. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")"
DRY_RUN=0
case "${1:-}" in
    "") ;;
    -n|--dry-run) DRY_RUN=1 ;;
    *) echo "Usage: $0 [--dry-run]" >&2; exit 2 ;;
esac
find . -type f -exec sed -i 's/\r$//' {} +   # in case the files come from a CRLF checkout

CHANGED=0
put() {   # put <new file> <destination> <mode>; sets CHANGED=1 if it was different
    local src=$1 dest=$2 mode=$3
    CHANGED=0
    if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
        echo "  unchanged  $dest"
        return 0
    fi
    echo "  update     $dest"
    CHANGED=1
    if [ "$DRY_RUN" = 1 ]; then return 0; fi
    if [ "$mode" = vfat ]; then
        cat "$src" > "$dest"   # /boot/firmware is FAT: no permissions to set
    else
        install -D -m "$mode" "$src" "$dest"
    fi
}

echo "Installing configuration on $(hostname):"
REBOOT=0

# Boot settings: switch off audio, camera and display detection and the KMS driver, then
# append config.txt.append (replacing an earlier copy of it). No overclocking.
CFG=/boot/firmware/config.txt
if [ -f "$CFG" ]; then
    NEW="$(mktemp)"
    trap 'rm -f "$NEW"' EXIT
    sed -e 's/^dtparam=audio=on/dtparam=audio=off/' \
        -e 's/^camera_auto_detect=1/camera_auto_detect=0/' \
        -e 's/^display_auto_detect=1/display_auto_detect=0/' \
        -e 's/^dtoverlay=vc4-kms-v3d/#dtoverlay=vc4-kms-v3d/' \
        -e 's/^max_framebuffers=2/#max_framebuffers=2/' \
        -e '/# --- printserver tuning ---/,$d' "$CFG" > "$NEW"
    cat config.txt.append >> "$NEW"
    if [ "$DRY_RUN" = 0 ] && [ ! -f "$CFG.orig" ]; then cp "$CFG" "$CFG.orig"; fi   # first-run backup
    put "$NEW" "$CFG" vfat
    [ "$CHANGED" = 0 ] || REBOOT=1
else
    echo "  skipped    $CFG (not a Raspberry Pi OS boot partition)"
fi

put hdmi-off.service /etc/systemd/system/hdmi-off.service 644   # takes effect at boot
if [ "$DRY_RUN" = 0 ]; then
    [ "$CHANGED" = 0 ] || systemctl daemon-reload
    systemctl is-enabled --quiet hdmi-off.service || systemctl enable --quiet hdmi-off.service
fi

put journald-size.conf /etc/systemd/journald.conf.d/size.conf 644
if [ "$DRY_RUN" = 0 ] && [ "$CHANGED" = 1 ]; then systemctl restart systemd-journald; fi

if [ -d /etc/avahi/services ]; then
    # Avahi notices new or changed files by itself
    for f in avahi/printserver-*.service; do
        put "$f" "/etc/avahi/services/$(basename "$f")" 644
    done
else
    echo "  skipped    Bonjour records (avahi-daemon not installed; run install/01-print-server.sh)"
fi

if [ "$DRY_RUN" = 1 ]; then
    echo "Dry run: nothing was changed."
elif [ "$REBOOT" = 1 ]; then
    echo "Boot settings changed: reboot the Pi to apply them (sudo reboot)."
fi
