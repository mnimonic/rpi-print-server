#!/bin/bash
# Optional setup step 5: run the Pi on Wi-Fi only, with automatic rollback.
#
# Turns Ethernet autoconnect off (eth0 and wlan0 on the same subnet break inbound
# connections to the Wi-Fi address: replies leave via eth0) and sets up the Wi-Fi profile:
#   - DHCP (default): the router assigns the address. It may change, but <hostname>.local,
#     the Windows Network entry (SSDP), Bonjour and the welcome page QR codes follow it.
#   - Fixed IP (IP=... GW=...): only if your router can reserve it or it's outside the
#     router's DHCP pool.
#
# Usage (as root):
#   sudo WIFI_PROFILE=wlan0-MyWifi ./install/05-wifi-only.sh                                    # DHCP
#   sudo WIFI_PROFILE=wlan0-MyWifi IP=192.168.1.50/24 GW=192.168.1.1 ./install/05-wifi-only.sh  # fixed
# (WIFI_PROFILE is the file name in /etc/NetworkManager/system-connections without .nmconnection)
#
# Runs detached so it survives the SSH session dropping; afterwards reconnect to
# http://<hostname>.local/ (or the fixed IP). Log: /root/wifi-only.log
set -euo pipefail
: "${WIFI_PROFILE:?set WIFI_PROFILE (file name without .nmconnection)}"
IP="${IP:-}"
GW="${GW:-}"
if [ -n "$IP" ] && [ -z "$GW" ]; then echo "With IP=, set GW= (the router) too" >&2; exit 1; fi
DNS="${DNS:-1.1.1.1;$GW;}"
ETH_PROFILE="${ETH_PROFILE:-eth0}"

if [ -z "${DETACHED:-}" ]; then
  DETACHED=1 WIFI_PROFILE="$WIFI_PROFILE" IP="$IP" GW="$GW" DNS="$DNS" ETH_PROFILE="$ETH_PROFILE" \
    setsid nohup bash "$0" >/dev/null 2>&1 < /dev/null &
  if [ -n "$IP" ]; then TARGET="${IP%/*}"; else TARGET="$(hostname).local"; fi
  echo "Switching in the background; reconnect in ~30 s to http://$TARGET/"
  exit 0
fi

exec >/root/wifi-only.log 2>&1
D=/etc/NetworkManager/system-connections
B=/root/nm-backup-$(date +%s)
mkdir -p "$B" && cp -a $D/*.nmconnection "$B/"
echo "backup in $B"

# Edit the keyfiles directly: `nmcli connection modify` would write them back to netplan
python3 - "$D/$ETH_PROFILE.nmconnection" "$D/$WIFI_PROFILE.nmconnection" "$IP" "$GW" "$DNS" "$(hostname)" <<'PY'
import configparser, os, sys
eth, wifi, ip, gw, dns, host = sys.argv[1:]
def edit(path, changes, drop=()):
    c = configparser.ConfigParser(interpolation=None)
    c.optionxform = str
    c.read(path)
    for sec, keys in drop:
        if c.has_section(sec):
            for k in [k for k in c[sec] if k.startswith(keys)]:
                del c[sec][k]
    for sec, kv in changes.items():
        if not c.has_section(sec):
            c.add_section(sec)
        for k, v in kv.items():
            c[sec][k] = v
    with open(path, "w") as f:
        c.write(f, space_around_delimiters=False)
if os.path.exists(eth):
    edit(eth, {"connection": {"autoconnect": "false"}})
if ip:
    ipv4 = {"method": "manual", "address1": f"{ip},{gw}", "dns": dns}
else:
    # Send the hostname so the Pi shows up by name in the router's client list
    ipv4 = {"method": "auto", "dhcp-send-hostname": "true", "dhcp-hostname": host}
edit(wifi, {
    "connection": {"autoconnect": "true", "autoconnect-priority": "10"},
    "ipv4": ipv4,
    "wifi": {"powersave": "2"},   # 2 = power saving off (servers miss connections otherwise)
}, drop=[("ipv4", ("address", "gateway", "dns", "method", "dhcp-"))])
PY
chmod 600 $D/*.nmconnection

ETH_ID=$( [ -f "$D/$ETH_PROFILE.nmconnection" ] && sed -n 's/^id=//p' "$D/$ETH_PROFILE.nmconnection" || true)
WIFI_ID=$(sed -n 's/^id=//p' "$D/$WIFI_PROFILE.nmconnection")
sleep 2
nmcli connection reload
[ -n "$ETH_ID" ] && { nmcli connection down "$ETH_ID" || true; }
nmcli connection up "$WIFI_ID"

for i in $(seq 1 18); do
  G="${GW:-$(ip -4 route show default dev wlan0 | awk '{print $3; exit}')}"
  if [ -n "$G" ] && ping -c1 -W2 -I wlan0 "$G" >/dev/null 2>&1; then
    echo "SUCCESS: wlan0 reaches gateway $G"; ip -4 -br a; exit 0
  fi
  sleep 5
done

echo "FAILED: rolling back"
cp -a "$B"/*.nmconnection $D/
nmcli connection reload
nmcli connection up "$WIFI_ID" || true
[ -n "$ETH_ID" ] && { nmcli connection up "$ETH_ID" || true; }
ip -4 -br a
