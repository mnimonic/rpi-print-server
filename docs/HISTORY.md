# Project history

How this project was built, in order. Installation-specific values (IPs,
passwords, Wi-Fi details, MAC addresses, serial numbers) are left out on purpose.

## 1. Print server

- Starting point: a Raspberry Pi 1 Model B+ (ARMv6, 700 MHz, 512 MB) on Raspberry
  Pi OS 13 "trixie", reachable over SSH on Ethernet. Goal: a USB print server for
  an HP DeskJet 1515.
- Installed `cups`, `hplip`, `printer-driver-hpcups`, `avahi-daemon` (~20 min on a Pi 1).
- Enabled sharing and remote admin with `cupsctl`. `--no-remote-printers` isn't
  supported by this CUPS version and aborted the first script run.

## 2. Speed, services and power ("make it fast, cool, no overclock")

- Disabled bluetooth, cloud-init (5 units), udisks2, the serial console,
  rpi-eeprom-update, NetworkManager-wait-online, colord/saned, and the man-db and
  apt-daily timers.
- Headless `config.txt`: KMS overlay off, audio/camera/display detection off,
  `gpu_mem=16`, HDMI off via `vcgencmd display_power 0`. RAM available went from
  about 284 MB to 356 MB.
- Tried `arm_freq_min=300` for idle down-clocking. The firmware ignores it at the
  stock clock on a Pi 1, so it was removed. Later explained in detail; the owner
  kept stock 700 MHz.
- Boot took 4 min: NetworkManager re-ran `netplan generate` about 4 times at
  ~30 s each. Moved the profiles to native NM keyfiles and moved the netplan YAML
  aside, which brought boot to about 1 min 50 s.

## 3. CUPS fixes

- The CUPS web UI didn't load: `WebInterface No`, and `cups.service` only starts
  through `printer.target` (when a USB printer is detected). Fixed both;
  verified across a reboot.

## 4. Adding the printer

- Added the queue `DeskJet_1515` before the printer was plugged in, with the
  generic `usb://` URI, the hpcups "HP Deskjet 1510 Series" driver, A4, shared,
  default, and `retry-job` error policy.
- Windows 11: `Add-Printer -IppURL` needed admin rights, and the auto-mode
  classifier blocked starting an elevated PowerShell. The owner ran it themselves
  (Microsoft IPP Class Driver).
- When plugged in, the printer showed up as `03f0:c111`, but only the `hp:`
  backend listed it. Switching the queue to the `hp:` URI was stopped by the owner
  because printing already worked through the `usb://` URI.

## 5. Claude Code health check (`/doctor`)

- Nothing to fix: npm-global install up to date, auto mode already the default,
  no CLAUDE.md files, no denials.

## 6. Wi-Fi

- Discussed adapters: anything driver-free/AIC8800 has no mainline driver. Cudy
  WU900 and Andowl AX600 depend on their chip, so verify with `lsusb`. Cheap
  in-kernel options: MT7601U, RTL8188EU (TL-WN725N), RTL8192EU (TL-WN823N).
- Installed a **TP-Link TL-WN823N** (RTL8192EU, `rtl8xxxu`); it worked immediately.
- SSH to the Wi-Fi address failed while Ethernet was also up (same subnet,
  replies left through eth0). Moved the fixed IP to wlan0, turned off Ethernet
  autoconnect and Wi-Fi power saving, using a detached script with automatic
  rollback. Verified across a reboot with the cable removed.
- Discussed the Pi as a DNS cache (dnsmasq). Measured Cloudflare at about 20 ms
  from the PC: little gain, so not done.

## 7. Scanning

- `libsane-hpaio` sees the scanner. Built the stdlib-only web page `scanweb.py`
  on port 8080, running as a restricted `scanweb` user.
- Bugs found and fixed, in order:
  1. Lineart failed ("too few scanlines"). At first Lineart was removed and a
     retry on "Device busy" added.
  2. PDFs opened blank: `scanimage --format=pdf` writes `/Length 0` and a
     zeroed xref. Fix: scan JPEG and build the PDF ourselves.
  3. Owner's idea: retry Lineart now that the PDF bug was known. Lineart works
     as raw PNM; its PDF/PNG writers fail. Fix: scan PNM and encode 1-bit
     PDF (Flate) or PNG ourselves. Lineart came back.
  4. Lineart at 75 dpi was unreadable. Shown to come from the scanner itself
     (raw output, compared with greyscale), not the script.
  5. Added "Black & white – enhanced": greyscale scan thresholded at 200.
     Keeps light-grey text and is readable at 75 dpi. Lineart and its dpi
     options were kept, as the owner asked.
  6. At 600 dpi hpaio delivered one row fewer than the header said, which
     crashed the converter. It now uses the rows received, and errors are shown
     on the page.
- Scan settings are remembered in a cookie.

## 8. Repository

- Created `rpi-print-server` with numbered install scripts, a README, an LF-only
  `.gitattributes`, and a `.gitignore` that blocks `.nmconnection` files.
  Scanned for secrets before every commit.

## 9. Welcome page and hardware monitoring

- `welcome.py` on port 80: printer status (detects a switched-off printer via
  the USB bus in sysfs), job queue, links, and Pi health: CPU and temperature
  graphs, clocks, voltages, under-voltage and throttling flags, memory, SD card,
  Wi-Fi, USB devices, firmware/OS/kernel. Runs as a `DynamicUser` with
  CAP_NET_BIND_SERVICE and the video group.
- It immediately showed `throttled=0x50005`: under-voltage happening at that
  moment, after the Wi-Fi adapter was added. Later the Pi restarted during a
  3-minute under-voltage episode.
- The owner replaced the power supply with a **5.25 V / 2.1 A** one. Result: boot
  `throttled=0x0`, and 0 of 60 seconds with the under-voltage alarm under full
  CPU and Wi-Fi load. CPU speed was 700 MHz before and after; the gain is
  stability, not speed.

## 10. Making the server easy to find

- The owner asked for an easy way to find the server's address on Android, iOS,
  Windows and Mac.
- `<hostname>.local` already worked through Avahi (verified from Windows 11).
- Added to `welcome.py` (stdlib only): an SSDP/UPnP responder, so the server shows
  in Windows File Explorer → Network and double-clicking opens the welcome page;
  a hand-written QR encoder; an "Open on another device" card; and `/sheet`, a
  printable page with QR codes for the welcome and scan pages. The QR encoder was
  verified with zxing-cpp across versions 1–10.
- Added Avahi `_http._tcp` records for both pages (`config/avahi/`).
- Router DHCP reservation: the router has no reservation feature (only static ARP
  binding), so this is still open.
