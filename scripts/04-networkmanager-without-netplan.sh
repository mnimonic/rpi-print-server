#!/bin/bash
# Move network profiles from netplan to native NetworkManager keyfiles.
#
# On Raspberry Pi OS (trixie) NetworkManager re-runs `netplan generate` several
# times during boot; on a Pi 1 each run takes ~30 s, which made boot take ~4 min.
# Copying the generated profiles to /etc/NetworkManager/system-connections and
# removing the netplan YAML cut boot to under 2 min.
#
# After this, edit the .nmconnection files directly and run
# `nmcli connection reload` -- `nmcli connection modify` would write the
# profile back into /etc/netplan and bring the slowdown back.
#
# Run on the Pi as root:  sudo ./04-networkmanager-without-netplan.sh
set -euo pipefail
SRC=/run/NetworkManager/system-connections
DST=/etc/NetworkManager/system-connections

shopt -s nullglob
for f in "$SRC"/netplan-*.nmconnection; do
  name=$(basename "$f"); name=${name#netplan-}
  cp "$f" "$DST/$name"
  echo "copied $name"
done
chmod 600 "$DST"/*.nmconnection

mkdir -p /root/netplan-backup
mv /etc/netplan/*.yaml /root/netplan-backup/ 2>/dev/null || true
echo "netplan YAML moved to /root/netplan-backup. Reboot to check."
