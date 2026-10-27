#!/bin/bash
# Move the Pi's fixed IP from Ethernet to Wi-Fi, with automatic rollback.
#
# Having eth0 and wlan0 on the same subnet breaks inbound connections to the
# Wi-Fi address (replies leave via eth0), so Ethernet autoconnect is turned off.
#
# Usage (as root, ideally while connected over Ethernet):
#   sudo IP=192.168.1.50/24 GW=192.168.1.1 WIFI_PROFILE=wlan0-MyWifi ./05-wifi-only.sh
#
# Runs detached so it survives the SSH session dropping when eth0 goes down.
# Log: /root/wifi-switch.log
set -euo pipefail
: "${IP:?set IP, e.g. 192.168.1.50/24}"
: "${GW:?set GW, e.g. 192.168.1.1}"
: "${WIFI_PROFILE:?set WIFI_PROFILE (file name without .nmconnection)}"
DNS="${DNS:-1.1.1.1;$GW;}"
ETH_PROFILE="${ETH_PROFILE:-eth0}"

if [ -z "${DETACHED:-}" ]; then
  DETACHED=1 IP="$IP" GW="$GW" WIFI_PROFILE="$WIFI_PROFILE" DNS="$DNS" ETH_PROFILE="$ETH_PROFILE" \
    setsid nohup "$0" >/dev/null 2>&1 < /dev/null &
  echo "Switching in the background; reconnect to ${IP%/*} over Wi-Fi in ~30 s."
  exit 0
fi

exec >/root/wifi-switch.log 2>&1
D=/etc/NetworkManager/system-connections
B=/root/nm-backup-$(date +%s)
mkdir -p "$B" && cp -a $D/*.nmconnection "$B/"
echo "backup in $B"

python3 - "$D/$ETH_PROFILE.nmconnection" "$D/$WIFI_PROFILE.nmconnection" "$IP" "$GW" "$DNS" <<'PY'
import configparser, sys
eth, wifi, ip, gw, dns = sys.argv[1:]
def edit(path, changes):
    c = configparser.ConfigParser(interpolation=None)
    c.optionxform = str
    c.read(path)
    for sec, kv in changes.items():
        if not c.has_section(sec):
            c.add_section(sec)
        for k, v in kv.items():
            c[sec][k] = v
    with open(path, "w") as f:
        c.write(f, space_around_delimiters=False)
edit(eth, {"connection": {"autoconnect": "false"}})
edit(wifi, {
    "connection": {"autoconnect": "true", "autoconnect-priority": "10"},
    "ipv4": {"method": "manual", "address1": f"{ip},{gw}", "dns": dns},
    "wifi": {"powersave": "2"},   # 2 = power saving off (servers miss connections otherwise)
})
PY
chmod 600 $D/*.nmconnection

ETH_ID=$(sed -n 's/^id=//p' "$D/$ETH_PROFILE.nmconnection")
WIFI_ID=$(sed -n 's/^id=//p' "$D/$WIFI_PROFILE.nmconnection")
sleep 2
nmcli connection reload
nmcli connection down "$ETH_ID" || true
nmcli connection up "$WIFI_ID"

for i in $(seq 1 18); do
  if ping -c1 -W2 -I wlan0 "$GW" >/dev/null 2>&1; then
    echo "SUCCESS: wlan0 reaches gateway"; ip -4 -br a; exit 0
  fi
  sleep 5
done

echo "FAILED: rolling back"
cp -a "$B"/*.nmconnection $D/
nmcli connection reload
nmcli connection up "$WIFI_ID" || true
nmcli connection up "$ETH_ID" || true
ip -4 -br a
