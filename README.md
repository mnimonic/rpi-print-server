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
- **Scanning**: a small web page (`http://<pi>:8080`) with a Scan button, 75–2400 dpi.
  Scans are saved on the Pi and listed for download, so a closed browser or a dropped
  connection loses nothing. Pure Python standard library; optional text recognition
  (OCR, English and Greek) with Tesseract gives searchable PDFs or plain text.
- **Lean and cool**: unneeded services disabled, headless boot config, no overclock.
- **Wi-Fi only** (optional): Ethernet off; fixed IP or DHCP on Wi-Fi.
- **Easy to find**: `http://<hostname>.local/`, an entry in Windows File Explorer →
  Network (SSDP/UPnP), Bonjour records for both pages, and QR codes plus a printable
  info sheet for phones.

## How the repository is organised

The files are split by **what they are** and **where they run**:

```
install/   Installation: one-time setup of the Pi's operating system (runs on the Pi)
config/    Configuration: system settings files and their installer (runs on the Pi)
app/       Application: the welcome page and the scan page, and their installer (runs on the Pi)
deploy/    Deployment: the scripts you run on YOUR computer to send the three above to the Pi
```

You normally only run the scripts in `deploy/`. Each one copies a folder to the Pi over
SSH and runs the installer inside it:

| You want to... | Run on your computer | What runs on the Pi |
|---|---|---|
| Set up a new Pi (everything) | `deploy/deploy-install.sh` | `install/install-all.sh` → steps 01–05, `config/install.sh`, `app/install.sh` |
| Change a setting in `config/` | `deploy/deploy-config.sh` | `config/install.sh` |
| Release or test a change in `app/` | `deploy/deploy-app.sh` | `app/install.sh` (inside the package) |

All installers are **safe to run again**: they compare each file with the one already on the
Pi, change only what differs, and print `unchanged` / `update` for every file. The config
and app deploy scripts have `--dry-run` to show what would change without changing anything.

<details>
<summary>Every file, one by one</summary>

| Path | What it is |
|---|---|
| `install/install-all.sh` | Runs the whole setup in order (steps below, then config and app) |
| `install/01-print-server.sh` | Installs CUPS, HPLIP, SANE, Avahi; enables sharing and the CUPS web UI |
| `install/02-add-printer.sh` | Adds and shares the DeskJet queue (A4, retry on error) |
| `install/03-tune-headless.sh` | Disables services a headless print server doesn't need |
| `install/04-networkmanager-without-netplan.sh` | Moves network profiles from netplan to NetworkManager keyfiles (boot 4 min → under 2 min) |
| `install/05-wifi-only.sh` | Optional: Wi-Fi only, DHCP (or a fixed IP), automatic rollback |
| `config/install.sh` | Installs the files below |
| `config/config.txt.append` | Headless boot settings added to `/boot/firmware/config.txt` (no overclock) |
| `config/hdmi-off.service` | Turns HDMI output off at boot |
| `config/journald-size.conf` | Limits the system log to 30 MB (less SD card wear) |
| `config/avahi/printserver-*.service` | Bonjour (`_http._tcp`) records for the welcome and scan pages |
| `config/wifi.nmconnection.example` | Template Wi-Fi profile; not installed automatically (it would hold your Wi-Fi password) |
| `app/install.sh` | Installs/updates the two services below, restarts what changed, checks they run |
| `app/welcome/welcome.py` | The welcome/status page (port 80), SSDP responder and QR encoder |
| `app/welcome/welcome.service` | Its systemd unit (throwaway `DynamicUser`, may only bind port 80) |
| `app/scanweb/scanweb.py` | The scan web page (port 8080) |
| `app/scanweb/scanweb.service` | Its systemd unit (runs as a restricted `scanweb` user) |
| `deploy/deploy-install.sh` | Full setup of a new Pi from your computer |
| `deploy/deploy-config.sh` | Sends `config/` to the Pi |
| `deploy/deploy-app.sh` | Builds a release package of `app/` (and tags it), sends it to the Pi |
| `deploy/lib.sh` | Shared SSH and packaging code for the three scripts above |

</details>

## Setup

**New to this?** Follow the short step-by-step guide in [docs/INSTALL.md](docs/INSTALL.md).
The rest of this section explains the same steps in more detail.

### What you need

- A Raspberry Pi (tested: Pi 1 Model B+), a microSD card (8 GB or more), and a good 5 V
  power supply (2.5 A recommended, see [Problems](#problems-found-and-how-theyre-handled)).
- An Ethernet cable for the first setup (Wi-Fi can be set up later, see step 5).
- The printer, connected by USB. It can be switched off during setup.
- A computer with **Git** and a **Bash** shell: Linux, macOS, or Windows with
  [Git for Windows](https://gitforwindows.org/) (use the "Git Bash" app it installs). On
  Windows, also install [PuTTY](https://www.putty.org/) if you want to log in with a
  password (its `plink` tool sends it).

### 1. Prepare the SD card

1. Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on your computer.
2. Choose your Pi model and **Raspberry Pi OS Lite** (no desktop; on a Pi 1 the 32-bit one).
3. When it asks about OS customisation, choose **Edit settings** and set:
   - a **hostname**, e.g. `printserver` (the Pi will be reachable as `printserver.local`),
   - a **username and password** (you need them below),
   - under *Services*: **Enable SSH** with password authentication.
   - Wi-Fi is optional here. If you set it, you can switch the Pi to Wi-Fi only in step 5.
4. Write the card, put it in the Pi, connect Ethernet and power. The first boot takes a few
   minutes.

### 2. Check that you can reach the Pi

In Git Bash (or a terminal):

```bash
ping printserver.local          # use your hostname; stop with Ctrl+C
ssh pi@printserver.local        # use your username; answer "yes" once, then type "exit"
```

If `ping` doesn't find it, look up the Pi's IP address in your router's list of devices and
use that instead of `printserver.local` everywhere below.

On Windows with PuTTY, also connect once with `plink pi@printserver.local` (or the PuTTY
app) and accept the host key: the scripts can't answer that question for you.

### 3. Install everything

Get this repository on your computer and run the full setup:

```bash
git clone <this repository's URL> rpi-print-server
cd rpi-print-server
PI_HOST=printserver.local PI_USER=pi PI_PASS='your password' ./deploy/deploy-install.sh
```

- `PI_HOST`, `PI_USER` and `PI_PASS` tell the script how to log in. They are only used for
  this command and are never saved. Leave out `PI_PASS` if you use SSH keys; sudo will then
  ask for the password.
- Add `PAPER=Letter` if you don't use A4 paper.

It takes 20–40 minutes on a Pi 1 (mostly downloading and installing packages) and prints a
heading for each of the 7 steps. Keep the computer awake until it says `Setup finished`.
If it stops with an error, fix the cause and run the same command again; every step can be
repeated safely.

### 4. Reboot and test

```bash
ssh pi@printserver.local sudo reboot
```

After about 2 minutes, open `http://printserver.local/` in a browser. You should see the
welcome page with the printer status; `http://printserver.local:8080/` is the scan page.
Then add the printer on your computers (see [Using it](#using-it)).

### 5. Optional: Wi-Fi only

Do this while the Pi is still on Ethernet, and only if the Pi already has a Wi-Fi profile
(set in Raspberry Pi Imager, or made from `config/wifi.nmconnection.example`). Find the
profile's name:

```bash
ssh pi@printserver.local ls /etc/NetworkManager/system-connections/
# e.g. wlan0-MyWifi.nmconnection  ->  the profile name is wlan0-MyWifi
```

Then run the full setup again with `WIFI_PROFILE` set (the other steps find nothing to do,
apart from re-checking the packages):

```bash
PI_HOST=printserver.local PI_USER=pi PI_PASS='...' WIFI_PROFILE=wlan0-MyWifi ./deploy/deploy-install.sh
```

When it says the network is switching, unplug the Ethernet cable, wait ~30 s, check that
`http://printserver.local/` opens, and reboot. If Wi-Fi doesn't work, the Pi restores the
previous settings by itself after about 90 s.

The Wi-Fi uses DHCP, so the Pi's address may change, but `http://<hostname>.local/`, the
Bonjour and SSDP announcements and the welcome page QR codes follow it, and computers
should add the printer by that name. For a fixed IP instead, run
`sudo WIFI_PROFILE=wlan0-MyWifi IP=192.168.1.50/24 GW=192.168.1.1 ./install/05-wifi-only.sh`
on the Pi, and reserve that IP for the Pi in your router (or pick one outside the router's
DHCP pool) so nothing else gets the same address.

### Without a second computer

Everything can also be run on the Pi itself, from a copy of the repository:

```bash
sudo apt-get install -y git
git clone <this repository's URL> rpi-print-server && cd rpi-print-server
sudo ./install/install-all.sh          # PAPER=... and WIFI_PROFILE=... work here too
sudo reboot
```

For later changes: `git pull`, then `sudo ./config/install.sh` and/or `sudo ./app/install.sh`.

## Making changes later

Edit the files on your computer, then send them with the matching deploy script. Set the
connection once per terminal so you don't have to repeat it:

```bash
export PI_HOST=printserver.local PI_USER=pi PI_PASS='...'
```

**Configuration** (`config/`) has no versions: the files as they are now get installed.

```bash
./deploy/deploy-config.sh --dry-run    # shows which files would change
./deploy/deploy-config.sh              # installs them; says if a reboot is needed
```

**Application** (`app/`): test your change first, then release it.

```bash
./deploy/deploy-app.sh --dry-run       # which files would change
./deploy/deploy-app.sh                 # test: installs your working tree as it is now
git commit -am "Describe the change"
./deploy/deploy-app.sh v1.4.0 "Describe the change"   # release: tag, build, install
git push --follow-tags                 # publish the commit and the tag
```

- A test deploy shows a version like `v1.3.0-2-gabc1234-dirty` in the welcome page footer
  (2 commits after v1.3.0, plus uncommitted changes). A release shows `v1.4.0`.
- A release builds `dist/rpi-print-server-v1.4.0.tar.gz` from the **committed** files only,
  so it refuses to run with uncommitted changes or a tag that already exists. Versions
  follow `vMAJOR.MINOR.PATCH`: raise PATCH for fixes, MINOR for new features.
- To go back to an earlier release: `./deploy/deploy-app.sh dist/rpi-print-server-v1.3.0.tar.gz`.
- `--no-deploy` only builds (and tags) the package. To install a package by hand, copy it to
  the Pi and run `tar -xzf rpi-print-server-v1.4.0.tar.gz && sudo ./rpi-print-server-v1.4.0/install.sh`.

The app installer restarts only the services whose files changed and checks that they are
running; if one doesn't start, it shows the last lines of its log.

**Installation steps** (`install/`): run `./deploy/deploy-install.sh` again. It repeats all
steps, which is safe but slow (it runs `apt-get`).

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

**CUPS admin:** `http://<pi>:631`. Log in with the Pi user you set up (`PI_USER`).

**Add the printer on Windows 11** (admin PowerShell):

```powershell
Add-Printer -IppURL "http://<pi>:631/printers/DeskJet_1515" -Name "HP DeskJet 1515 (Pi)"
```

Or go to Settings → Printers & scanners → Add device → *Add manually* → *Select a
shared printer by name* → `http://<pi>:631/printers/DeskJet_1515`, and choose the
**Microsoft IPP Class Driver**. No HP software is needed on the PC. macOS, iOS and
Android find it automatically.

**Scanning:** open `http://<pi>:8080`, place the page face-down in the top-left
corner of the glass, choose the settings and press **Scan**. The scan runs on the Pi
and the page shows its progress; the file downloads when done. Every scan is also saved
on the Pi (`/var/lib/scanweb/scans`) and listed under **Saved scans** with Download and
Delete, so you can close the page, lose the connection or fetch the file from another
device. Saved scans are kept until you delete them, one by one or with **Clean up**,
which deletes everything except the newest 10, 20 or 30 scans, or except the last 30
days (both ask for confirmation first). The list shows the free space, and a scan is
refused if the SD card would run low. Selections are remembered in a cookie.

| Colour option | How it is produced | Best for |
|---|---|---|
| Colour | Scanner colour mode (JPEG transfer) | Photos, colour documents |
| Greyscale | Scanner grey mode | Documents with shading |
| Black & white (smallest files) | Scanner's own Lineart mode | Plain black text at ≥200 dpi |
| Black & white – enhanced | Greyscale scan, thresholded on the Pi | Most documents; keeps light-grey text, readable even at 75 dpi |

Resolutions: 75–2400 dpi. 1200 dpi is the scanner's optical limit; 2400 dpi is
interpolated by the driver. Output can be PDF, JPEG or PNG; for black & white, JPEG is
replaced by PNG. Measured times for an A4 page on a Pi 1 (mostly scanner time):

| Settings | Time | File |
|---|---|---|
| Colour, 200 dpi, PDF | 30 s | 0.3 MB |
| Colour, 1200 dpi, JPEG | 5 min 50 s | 15 MB (9920 × 14028 px) |
| Black & white – enhanced, 1200 dpi, PDF | 4 min 50 s | 0.8 MB |
| Black & white, 2400 dpi, PNG | 9 min | 2.2 MB (19840 × 28056 px) |

600 dpi takes about 100 s. Above 600 dpi the raw scan (up to 1.7 GB for colour at
2400 dpi) goes to the SD card and is converted a block of rows at a time, so it fits in
the Pi's RAM; colour and greyscale JPEGs are encoded with `cjpeg`. PNG in colour above
600 dpi works but gives very large files.

**Text recognition (OCR):** choose *PDF with searchable text* (the scan with an invisible
text layer, so you can search and copy from it) or *Text only* (a UTF-8 `.txt` file), and
the language: English + Greek, English, or Greek. Tesseract runs on the Pi. OCR scans at
no more than 300 dpi (higher is slower and doesn't read better) and is slow on a Pi 1:
an A4 page at 300 dpi takes about 2 min to read with English + Greek and 1.5 min with one
language, on top of the scan. Use 300 dpi and Greyscale or Black & white – enhanced for
the best results.

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
  same subnet, so replies left through eth0. Use one interface (see `install/05-wifi-only.sh`).
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
- **Above 600 dpi, `scanimage`'s JPEG writer fails** ("Application transferred too
  few scanlines"): hpaio delivers a few rows fewer than announced (14028 of 14031 at
  1200 dpi). The server scans raw PNM, fixes the row count in the header and encodes
  with `cjpeg` (which also refuses a short file).
- **`/tmp` is a RAM disk (238 MB)** on Raspberry Pi OS, too small for a 1200 dpi scan.
  Scans and work files go to `/var/lib/scanweb` (systemd `StateDirectory=`).
- **Tesseract is slower with its default threads on a single core**: OpenMP added
  a minute of system time (3.5 min instead of 2 min per page). The server runs it
  with `OMP_THREAD_LIMIT=1`.
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
