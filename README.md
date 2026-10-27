# Raspberry Pi print & scan server

Turns an old Raspberry Pi (tested on a **Pi 1 Model B+**, Raspberry Pi OS 13 "trixie")
into a network print and scan server for a USB **HP DeskJet 1515** (any DeskJet
1510-series model should work).

- **Welcome page** (`http://<pi>/`): live printer status, links to scanning and
  the print queue, and Pi health: CPU usage/temperature graphs, clock speed, core
  and RAM voltages, under-voltage/throttling detection, memory, SD card, Wi-Fi
  signal and hardware details.
- **Printing**: CUPS + HPLIP, shared on the LAN and discoverable by Windows, macOS,
  iOS and Android (Avahi/IPP).
- **Scanning**: a small web page (`http://<pi>:8080`) with a Scan button. Pure
  Python standard library, no extra packages.
- **Lean and cool**: unneeded services disabled, headless boot config, no overclock.
- **Wi-Fi only** (optional): Ethernet off; fixed IP or DHCP on Wi-Fi.
- **Easy to find**: `http://<hostname>.local/`, an entry in Windows File Explorer →
  Network (SSDP/UPnP), Bonjour records for both pages, and QR codes plus a printable
  info sheet for phones.

## Repository layout

Numbered scripts run **on the Pi** (first-time setup):

| Path | What it is |
|---|---|
| `scripts/01-print-server.sh` | Installs CUPS, HPLIP, SANE, Avahi; enables sharing and the CUPS web UI |
| `scripts/02-add-printer.sh` | Adds and shares the DeskJet queue (A4, retry on error) |
| `scripts/03-tune-headless.sh` | Disables unneeded services, headless `config.txt`, HDMI off |
| `scripts/04-networkmanager-without-netplan.sh` | Moves network profiles from netplan to native NetworkManager keyfiles (big boot-time win) |
| `scripts/05-wifi-only.sh` | Moves a fixed IP to Wi-Fi, turns Ethernet autoconnect off, auto-rollback |
| `scripts/06-install-scanweb.sh` | Installs the scan web page as a systemd service |
| `scripts/07-install-welcome.sh` | Installs the welcome page (port 80) and the Bonjour records for both pages |
| `scripts/08-wifi-dhcp.sh` | Switches the Wi-Fi profile to DHCP (for routers without address reservation), auto-rollback |

The other scripts run **on your computer** (updates after setup):

| Path | What it is |
|---|---|
| `scripts/release.sh` | Builds a release package (`dist/*.tar.gz`) and tags the version |
| `scripts/deploy.sh` | Uploads a release package to the Pi and installs it |
| `scripts/install-release.sh` | The installer inside each package (runs on the Pi as `install.sh`) |

The rest:

| Path | What it is |
|---|---|
| `welcome/welcome.py` | The welcome/status page, SSDP responder and QR encoder |
| `welcome/welcome.service` | Its systemd unit (throwaway `DynamicUser`, may only bind port 80) |
| `scanweb/scanweb.py` | The scan web server |
| `scanweb/scanweb.service` | Its systemd unit (runs as a restricted `scanweb` user) |
| `config/config.txt.append` | Lines appended to `/boot/firmware/config.txt` |
| `config/hdmi-off.service` | Turns HDMI output off at boot |
| `config/wifi.nmconnection.example` | Template Wi-Fi profile with a fixed IP |
| `config/avahi/printserver-*.service` | Bonjour (`_http._tcp`) records for the welcome and scan pages |

## Setup

Copy the repository to the Pi (e.g. `git clone` or `scp -r`), then:

```bash
cd rpi-print-server
chmod +x scripts/*.sh
sudo ./scripts/01-print-server.sh          # CUPS + HP drivers + sharing
sudo ./scripts/02-add-printer.sh           # add the DeskJet (PAPER=Letter to override A4)
sudo ./scripts/03-tune-headless.sh         # optional: slim down a headless Pi
sudo ./scripts/04-networkmanager-without-netplan.sh   # optional: faster boot on slow Pis
sudo ./scripts/06-install-scanweb.sh       # scan web page
sudo ./scripts/07-install-welcome.sh       # welcome page on port 80 + Bonjour records
sudo reboot
```

Optional, to run on Wi-Fi only (do this while still connected by Ethernet):

```bash
ls /etc/NetworkManager/system-connections/       # find your Wi-Fi profile name
sudo IP=192.168.1.50/24 GW=192.168.1.1 WIFI_PROFILE=wlan0-MyWifi ./scripts/05-wifi-only.sh
```

Then reserve that IP for the Pi's Wi-Fi MAC address in your router's DHCP
settings (or exclude it from the DHCP range), so nothing else gets the same address.
Some routers (e.g. Cudy M3600 firmware 2.3.x) have no DHCP reservation. Then either pick
a fixed IP *outside* the router's DHCP pool, or let DHCP assign the address:

```bash
sudo WIFI_PROFILE=wlan0-MyWifi ./scripts/08-wifi-dhcp.sh
```

With DHCP the address can change, so refer to the Pi by its mDNS name
(`http://<hostname>.local/`) and add printers on PCs by that name rather than the IP. The
Bonjour and SSDP announcements and the QR codes follow the new address automatically.

## Releasing and deploying changes

Once a Pi is set up, ship later changes from your computer (Linux, macOS, or Git Bash on
Windows) as a release package instead of copying the repository again.

**1. Release.** Commit your changes, then:

```bash
./scripts/release.sh v1.2.0 "Short description"   # tags the commit, builds the package
git push --follow-tags                             # publish the commit and the tag
```

This builds `dist/rpi-print-server-v1.2.0.tar.gz`, which contains the files that go onto
the Pi, `VERSION`, and `install.sh`. The version is stamped into `welcome.py` and shown in
the welcome page footer. The script refuses to tag uncommitted changes or reuse a tag.
Versions follow `vMAJOR.MINOR.PATCH`.

**2. Deploy.**

```bash
export PI_HOST=192.168.1.50 PI_USER=pi              # or edit the top of deploy.sh
./scripts/deploy.sh --dry-run dist/rpi-print-server-v1.2.0.tar.gz   # what would change
./scripts/deploy.sh dist/rpi-print-server-v1.2.0.tar.gz             # install it
```

Without a package argument, `deploy.sh` builds a snapshot of your working tree and deploys
that. The footer then shows e.g. `v1.2.0-3-gabc1234-dirty`, which is handy for testing
before a release. Never commit a real password into `deploy.sh`, because the repository is
public. Without `PI_PASS` it uses your SSH keys and sudo asks for the password. With
`PI_PASS` it needs PuTTY's `plink` (Windows) or `sshpass`.

The installer copies only the files that differ from the Pi's, restarts only the affected
services, checks that they're running (and shows their log if not), and skips components
that were never set up. Without `deploy.sh`, copy the package to the Pi and run:

```bash
tar -xzf rpi-print-server-v1.2.0.tar.gz && sudo ./rpi-print-server-v1.2.0/install.sh
```

## Using it

**Welcome page:** `http://<pi>/` is the starting point, with links to everything below.
It shows whether the printer is ready, printing or switched off. CUPS alone can't tell
switched off from idle for a USB printer, so the page also checks the USB bus. It also
shows a red or amber banner if the Pi reports under-voltage, which means the power
supply or cable is too weak. Status updates every 5 s; on a Pi 1 that costs about 0.1 s of CPU.

**Finding the server** from other devices:

| Where | How |
|---|---|
| Windows 10/11, macOS, iOS, Linux | `http://<hostname>.local/` (mDNS via Avahi) |
| Windows File Explorer → Network | Appears as *Print & Scan Server (&lt;hostname&gt;)*; double-click opens the welcome page. Needs network discovery on (Private network profile). |
| Any phone | Scan the QR code on the welcome page, or on the printed info sheet (`http://<pi>/sheet`, "Print an info sheet") |
| Bonjour browser apps, Linux file managers | `_http._tcp` records "Print & Scan Server on &lt;hostname&gt;" and "Scanner on &lt;hostname&gt;" |

The QR codes contain the `<hostname>.local` name, so a printed sheet keeps working when
DHCP gives the Pi a new address; the current IP is listed next to them as a fallback.
Android resolves `.local` names since Android 12; on older phones type the IP instead. SSDP and the QR encoder are built into `welcome.py`
(standard library only); the SSDP part answers searches on UDP 1900 and re-announces
every 10 minutes.

**CUPS admin:** `http://<pi>:631`. Log in with the Pi user from `01-print-server.sh`.

**Add the printer on Windows 11** (admin PowerShell):

```powershell
Add-Printer -IppURL "http://<pi>:631/printers/DeskJet_1515" -Name "HP DeskJet 1515 (Pi)"
```

Or go to Settings → Printers & scanners → Add device → *Add manually* → *Select a
shared printer by name* → `http://<pi>:631/printers/DeskJet_1515`, and choose the
**Microsoft IPP Class Driver**. No HP software is needed on the PC. macOS, iOS and
Android find it automatically.

**Scanning:** open `http://<pi>:8080`, place the page face-down in the top-left
corner of the glass, choose the settings and press **Scan**. The file downloads
when done. Selections are remembered in a cookie.

| Colour option | How it is produced | Best for |
|---|---|---|
| Colour | Scanner colour mode (JPEG transfer) | Photos, colour documents |
| Greyscale | Scanner grey mode | Documents with shading |
| Black & white (smallest files) | Scanner's own Lineart mode | Plain black text at ≥200 dpi |
| Black & white – enhanced | Greyscale scan, thresholded on the Pi | Most documents; keeps light-grey text, readable even at 75 dpi |

Resolutions: 75–600 dpi. 1200+ dpi colour would exhaust the Pi 1's RAM. Output
can be PDF, JPEG or PNG; for black & white, JPEG is replaced by PNG. On a Pi 1
a page takes about 25–30 s at 200 dpi and about 100 s at 600 dpi, mostly scanner time.

## Problems found and how they're handled

These are the non-obvious issues hit while building this on a Pi 1 with
Raspberry Pi OS trixie and HPLIP 3.22.10:

- **CUPS didn't start at boot without the printer.** `cups.service` is only
  wanted by `printer.target`, which starts when a USB printer is detected.
  Fix: `systemctl add-wants multi-user.target cups.service`.
- **CUPS web UI disabled by default** (`WebInterface No`). Fix: `cupsctl WebInterface=yes`.
- **4-minute boot.** NetworkManager's netplan integration re-runs
  `netplan generate` several times at boot (~30 s each on a Pi 1). Moving the
  profiles to native keyfiles brought boot to under 2 min. Edit keyfiles
  directly; `nmcli connection modify` writes back to `/etc/netplan`.
- **SSH over Wi-Fi failed while Ethernet was also connected**: both were on the
  same subnet, so replies left through eth0. Use one interface (see `05-wifi-only.sh`).
- **`scanimage --format=pdf` writes broken PDFs**: the image stream `/Length`
  and the xref table are all `0`, so most viewers show a blank page. The scan
  server scans JPEG/PNM and builds the PDF itself.
- **Lineart (black & white) through `scanimage`'s PDF/PNG writers fails**
  ("Application transferred too few scanlines" / "No IDATs written"); only raw
  PNM output works. The server scans PNM and encodes PDF/PNG itself (zlib).
- **The scanner's Lineart cut-off drops light-grey text** and is unreadable at
  75 dpi. "Black & white – enhanced" scans greyscale and thresholds at 200/255.
- **hpaio sometimes returns one row fewer than the PNM header announces** (at
  600 dpi). The converter uses the rows actually received.
- **"Device busy" for a few seconds after each scan.** The server retries.
- **Under-voltage after adding a Wi-Fi adapter.** `vcgencmd get_throttled`
  reported `0x50005` (under-voltage and throttling happening now), and the
  kernel logged `hwmon hwmon1: Undervoltage detected!` lasting several minutes
  around an unexpected restart. The welcome page warns about this. The fix is
  a proper 5 V / 2.5 A supply and a short, thick cable; a weak supply can
  crash the Pi and corrupt the SD card.
- **No idle down-clocking on a Pi 1.** The firmware tells Linux the CPU range
  is 700–700 MHz, so the `ondemand` governor has nothing to scale between.
  `arm_freq_min=300` is ignored at the stock clock: on a Pi 1, dynamic clocking
  is only enabled when overclocked. It hardly matters, because an idle CPU
  already sleeps between tasks, and the core voltage stays at 1.20 V either way,
  so a lower idle clock would save only a few hundredths of a watt. Idle
  temperature is about 35–40 °C. Alternatives if you still want it:
  - `arm_freq=500` for a fixed lower clock: slightly cooler, but about 30% slower.
  - `arm_freq=701` with `arm_freq_min=300`: a nominal 1 MHz "overclock" with no
    voltage change, which may unlock scaling. Untested.

## Wi-Fi adapters

A Pi 1 has no Wi-Fi. Adapters with in-kernel drivers work with no extra
installs, e.g. **TP-Link TL-WN823N** (RTL8192EU, tested) or **TL-WN725N**
(RTL8188EUS). Avoid "driver-free"/AIC8800 adapters, which have no mainline
Linux driver. The Pi 1's shared USB bus limits throughput to roughly
20–30 Mbps, which is plenty for printing.

A Wi-Fi adapter adds a noticeable current draw. Use a good 5 V / 2.5 A power
supply, or the Pi will report under-voltage (see the welcome page).

## Security notes

- The CUPS sharing, the scan page and the welcome page are open to anyone on your LAN. Don't
  expose ports 80/631/8080 (or UDP 1900, SSDP) to the internet.
- Change the Pi user's default password (`passwd`).
- Never commit real `.nmconnection` files: they contain the Wi-Fi password
  (`.gitignore` blocks them).
