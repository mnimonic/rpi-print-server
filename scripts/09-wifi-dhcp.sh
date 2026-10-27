#!/bin/bash
# Switch the Wi-Fi profile from a fixed IP to DHCP, with automatic rollback.
#
# With DHCP the router's DNS knows the Pi by name (e.g. rasp-printer.lan on many routers),
# which works on every device including Android. Use this when the router can't reserve
# an address. Everything else follows the Pi by name: <hostname>.local, the Windows
# Network entry (SSDP), the printer on phones (Bonjour) and the welcome page QR codes.
#
# Usage (as root):  sudo WIFI_PROFILE=wlan0-MyWifi ./09-wifi-dhcp.sh
#
# Runs detached, because the SSH session drops when the address changes.
# Afterwards find the Pi at http://<hostname>.lan/ or http://<hostname>.local/.
# Log: /root/wifi-dhcp.log
set -euo pipefail
: "${WIFI_PROFILE:?set WIFI_PROFILE (file name without .nmconnection)}"

if [ -z "${DETACHED:-}" ]; then
  DETACHED=1 WIFI_PROFILE="$WIFI_PROFILE" setsid nohup "$0" >/dev/null 2>&1 < /dev/null &
  echo "Switching to DHCP in the background; the Pi will be back in ~30 s at"
  echo "http://$(hostname).local/ (or http://$(hostname).lan/ on many routers)."
  exit 0
fi

exec >/root/wifi-dhcp.log 2>&1
D=/etc/NetworkManager/system-connections
F="$D/$WIFI_PROFILE.nmconnection"
B=/root/nm-backup-$(date +%s)
mkdir -p "$B" && cp -a $D/*.nmconnection "$B/"
echo "backup in $B"

# Edit the keyfile directly: `nmcli connection modify` would write it back to netplan
python3 - "$F" "$(hostname)" <<'PY'
import configparser, sys
path, host = sys.argv[1:]
c = configparser.ConfigParser(interpolation=None)
c.optionxform = str
c.read(path)
ipv4 = c["ipv4"]
for k in [k for k in ipv4 if k.startswith(("address", "gateway", "dns"))]:
    del ipv4[k]          # use the router's address, gateway and DNS
ipv4["method"] = "auto"
ipv4["dhcp-send-hostname"] = "true"
ipv4["dhcp-hostname"] = host   # the name the router's DNS will know us by
with open(path, "w") as f:
    c.write(f, space_around_delimiters=False)
PY
chmod 600 $D/*.nmconnection

WIFI_ID=$(sed -n 's/^id=//p' "$F")
sleep 2
nmcli connection reload
nmcli connection up "$WIFI_ID"

for i in $(seq 1 18); do
  GW=$(ip -4 route show default dev wlan0 | awk '{print $3; exit}')
  if [ -n "$GW" ] && ping -c1 -W2 -I wlan0 "$GW" >/dev/null 2>&1; then
    echo "SUCCESS: DHCP address on wlan0, gateway $GW reachable"; ip -4 -br a; exit 0
  fi
  sleep 5
done

echo "FAILED: rolling back"
cp -a "$B"/*.nmconnection $D/
nmcli connection reload
nmcli connection up "$WIFI_ID" || true
ip -4 -br a
