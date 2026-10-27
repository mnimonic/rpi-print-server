#!/bin/bash
# Slim down a headless Raspberry Pi 1 running as a print server:
# disable unneeded services, cut GPU memory, turn off HDMI/audio/camera.
# Run on the Pi as root:  sudo ./03-tune-headless.sh   (reboot afterwards)
set -euo pipefail

# Bluetooth (Pi 1 has none), cloud-init (first boot only), udisks2 (desktop
# automount), serial console, EEPROM updater (Pi 1 has no EEPROM),
# wait-online (slows boot), desktop-ish timers, scanner/colour daemons.
for u in bluetooth.service udisks2.service rpi-eeprom-update.service \
         serial-getty@ttyAMA0.service NetworkManager-wait-online.service \
         ModemManager.service colord.service saned.socket \
         man-db.timer apt-daily.timer apt-daily-upgrade.timer \
         cloud-init-local.service cloud-init-main.service cloud-init-network.service \
         cloud-config.service cloud-final.service cloud-init-hotplugd.socket; do
  systemctl disable --now "$u" 2>/dev/null || true
done
mkdir -p /etc/cloud && touch /etc/cloud/cloud-init.disabled

# Drop the serial console from the kernel command line
sed -i 's/console=serial0,[0-9]* //' /boot/firmware/cmdline.txt

# Keep the journal small (less SD card wear)
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=30M\n' > /etc/systemd/journald.conf.d/size.conf

# Boot config: headless, no overclock
CFG=/boot/firmware/config.txt
cp -n "$CFG" "$CFG.orig"
sed -i -e 's/^dtparam=audio=on/dtparam=audio=off/' \
       -e 's/^camera_auto_detect=1/camera_auto_detect=0/' \
       -e 's/^display_auto_detect=1/display_auto_detect=0/' \
       -e 's/^dtoverlay=vc4-kms-v3d/#dtoverlay=vc4-kms-v3d/' \
       -e 's/^max_framebuffers=2/#max_framebuffers=2/' "$CFG"
sed -i '/# --- printserver tuning ---/,$d' "$CFG"
cat "$(dirname "$0")/../config/config.txt.append" >> "$CFG"

# Turn HDMI output off at boot (less power and heat)
install -m 644 "$(dirname "$0")/../config/hdmi-off.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable hdmi-off.service

echo "Done. Reboot to apply the boot config."
