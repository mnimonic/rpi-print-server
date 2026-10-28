#!/bin/bash
# Setup step 3: slim down a headless Raspberry Pi 1 running as a print server by
# disabling services it doesn't need. (GPU memory, HDMI, audio and camera are turned off by
# the configuration files: config/install.sh.)
# Run on the Pi as root:  sudo ./install/03-tune-headless.sh   (reboot afterwards)
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

echo "Done. Reboot to apply."
