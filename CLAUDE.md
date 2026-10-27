# Project context for Claude Code

Raspberry Pi 1 B+ print & scan server for a USB HP DeskJet 1515. The README covers
features, setup and the problems found along the way, so read it first. This file holds
what the README doesn't: how to work on the live Pi, the owner's preferences, and the
current state. The full history is in `docs/HISTORY.md`. Details of the owner's own
installation (IP, username, MACs) are in `CLAUDE.local.md`, which is gitignored and may
be missing on other machines.

## Owner preferences (decided, don't re-litigate)

- **No overclocking.** Stock clocks/voltage only. Underclocking was offered and the owner
  chose to leave the CPU at stock 700 MHz.
- **Lean and fast.** Unneeded services are disabled (see `scripts/03-tune-headless.sh`).
  Avoid adding heavy dependencies; the Pi is a single-core ARMv6 at 700 MHz with 512 MB RAM.
  The web pages use only the Python standard library.
- **Wi-Fi only.** Ethernet autoconnect is off on purpose.
- **wlan0 uses DHCP** (since 2026-10-27, owner's choice: "let dhcp do its job"). The router has
  no reservation, so the address may change; use `<hostname>.local`, not a hard-coded IP.
  Switched with `scripts/05-wifi-only.sh` (DHCP is its default).
- **No router-specific features.** The owner rejected using the router's `.lan` DNS names
  (other users' routers differ). QR codes and the info sheet use `<hostname>.local` (works on the
  owner's Android); the IP is shown as a fallback.
- **Printer queue uses the generic `usb://HP/Deskjet%201510%20series` URI.** Switching to the
  `hp:/usb/...?serial=` URI was attempted and the owner stopped it (printing already worked).
  Don't change it without asking.
- **Scan page:** keep all four colour modes ("Black & white" Lineart and
  "Black & white – enhanced"), and keep every dpi option for Lineart too. Selections are
  remembered in a cookie (`scanprefs`).
- The repo is pushed to GitHub: **never commit secrets** (passwords, Wi-Fi PSK/SSID, MACs,
  printer serial, home IPs). Use placeholders like `<pi>` / `192.168.1.50`.

## Working on the live Pi from Windows

- The dev machine is Windows. SSH uses PuTTY: `plink`/`pscp` in `C:\Program Files\PuTTY`.
  Password auth (no keys set up yet).
- **Use the Bash tool, not PowerShell**, for remote commands. PowerShell mangles nested
  quotes, `|` and `$` inside `plink "..."` strings, and several commands silently didn't run
  because of this.
- Root commands pattern (password on stdin for `sudo -S`, script via heredoc):
  ```bash
  { echo "$PI_PASS"; cat <<'EOF'
  systemctl restart scanweb
  EOF
  } | "/c/Program Files/PuTTY/plink.exe" -batch -ssh -pw "$PI_PASS" <user>@<pi> "sudo -S -p '' bash -s"
  ```
  A harmless `<password>: command not found` line appears when sudo already has cached
  credentials; filter it out.
- **Release, then deploy** (from Git Bash): commit, run `./scripts/release.sh vX.Y.Z "msg"`
  (tags the commit, builds `dist/rpi-print-server-vX.Y.Z.tar.gz`), then
  `PI_HOST=... PI_USER=... PI_PASS=... ./scripts/deploy.sh [--dry-run] <package>`.
  `deploy.sh` without a package deploys a `-dirty` snapshot of the working tree for testing.
  Pass the credentials as env vars; never write them into `deploy.sh` (it's tracked).
- The list of deployed files lives in `scripts/release.sh` (`FILES`), and where each one is
  installed lives in `scripts/install-release.sh`. Keep the two in sync when adding a file.
- Pushing commits and tags to GitHub is the owner's call; ask before `git push`.
- `plink -batch` refuses unknown host keys, and the Pi's address can change (DHCP). The key
  for a new address can be copied from an existing `ssh-ed25519@22:<old ip>` value under
  `HKCU\Software\SimonTatham\PuTTY\SshHostKeys`; plink then still verifies it.
- For anything that drops the network (switching interfaces), run the change **detached with
  an automatic rollback** (`setsid nohup`; see `scripts/05-wifi-only.sh`).
- Long operations on the Pi 1 (apt, boot ~2 min, 600 dpi scans ~100 s) need long timeouts
  or background runs.
- Check that the Pi matches the repo with `./scripts/deploy.sh --dry-run` (all lines
  `unchanged`). A plain `md5sum` of `welcome.py` no longer matches, because the deployed
  copy has the stamped `VERSION` line.

## Deployed services on the Pi

| Service | Port | Runs as | Source |
|---|---|---|---|
| `cups` | 631 | root | distro (forced into `multi-user.target`) |
| `scanweb` | 8080 | `scanweb` (groups lp, scanner) | `scanweb/scanweb.py` |
| `welcome` | 80, UDP 1900 (SSDP) | `DynamicUser` + CAP_NET_BIND_SERVICE, group video | `welcome/welcome.py` |
| `hdmi-off` | – | oneshot | `config/hdmi-off.service` |
| `avahi-daemon` | 5353 | distro | + `config/avahi/printserver-*.service` in `/etc/avahi/services/` |

## Gotchas (beyond the README list)

- **`nmcli connection modify` writes the profile back into `/etc/netplan/`**, which brings
  back the 4-minute boot. Edit `/etc/NetworkManager/system-connections/*.nmconnection`
  directly (root-only, mode 600), then run `nmcli connection reload`.
- `vcgencmd` works without root (group `video`, `/dev/vchiq`) and takes ~15 ms per call.
  The mailbox `/dev/vcio` is root-only.
- `vcgencmd get_config int` omits settings left at the default on a Pi 1. Query them
  individually (`get_config arm_freq`), where `0` means stock.
- `welcome.py` builds its page with `{{placeholder}}` replacement, **not** `%` formatting:
  the CSS and JS contain literal `%`.
- In the scan code, `jpeg_to_pdf`/`pbm_to_pdf`/`pbm_to_png` are hand-written encoders
  (stdlib only) because `scanimage`'s PDF/PNG writers are broken. They were verified
  round-trip; keep them that way.
- `welcome.py` has a hand-written QR encoder (byte mode, level M, versions 1–10). It was
  verified by decoding 22 strings across all ten versions with zxing-cpp on the dev PC.
  Re-check the same way after any change.
- The Windows dev PC owns UDP 1900 (its SSDP service), so test SSDP against the Pi with
  an M-SEARCH from an ephemeral port, not by running the responder locally.
- Tests that need the scanner: there is a Windows test page on the glass, which works well
  as a reference image.

## Open items

- Change the Pi user's password from its weak initial value; set up SSH keys.
- Optional: Ethernet as an emergency fallback when Wi-Fi is down (offered, not done).
- Optional: ink levels on the welcome page (needs the `hp:` backend; see preferences).
- Optional: a LICENSE file (none chosen yet).
