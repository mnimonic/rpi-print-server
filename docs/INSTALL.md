# How to install

This turns a Raspberry Pi into a print and scan server for an HP DeskJet 1515.
It takes about an hour, most of it waiting.

## You need

- A Raspberry Pi, a microSD card (8 GB or more) and a good 5 V power supply
- An Ethernet cable
- The printer, plugged into the Pi by USB
- A computer with [Git](https://git-scm.com/downloads) (on Windows, use the **Git Bash** app it installs)
- On Windows, also [PuTTY](https://www.putty.org/). On Linux/macOS, `sshpass` (e.g. `sudo apt install sshpass`)

## Step 1: Prepare the SD card

1. Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) and open it.
2. Choose your Pi model and **Raspberry Pi OS Lite**.
3. When it asks, choose **Edit settings** and fill in:
   - **Hostname:** `printserver`
   - **Username** and **password** (write them down)
   - **Services** tab: tick **Enable SSH**
4. Write the card.

## Step 2: Start the Pi

Put the card in the Pi, connect the Ethernet cable, then the power.
Wait 5 minutes.

## Step 3: Check you can reach it

Open Git Bash (or a terminal) and type:

```bash
ssh pi@printserver.local
```

Use your own username instead of `pi`. Type `yes` if asked, then your password.
If you see a prompt like `pi@printserver:~ $`, it works. Type `exit`.

## Step 4: Install

Still in Git Bash, type these three lines (put in your own username and password):

```bash
git clone https://github.com/mnimonic/rpi-print-server.git
cd rpi-print-server
PI_HOST=printserver.local PI_USER=pi PI_PASS='your password' ./deploy/deploy-install.sh
```

Wait until it says **Setup finished** (20–40 minutes). Don't let the computer sleep.

## Step 5: Restart the Pi

```bash
ssh pi@printserver.local sudo reboot
```

Wait 2 minutes.

## Done

Open **http://printserver.local/** in a browser. You'll see the printer status and links to
scanning. To print from a phone or computer, add the printer it finds on the network called
**HP DeskJet 1515**.

## If something goes wrong

| Problem | What to do |
|---|---|
| `ssh` says it can't find `printserver.local` | Find the Pi's IP address in your router's device list and use it instead of `printserver.local` |
| The install stopped with an error | Run the same install command again. It continues safely |
| `Permission denied` | Check the username and password |
| On Windows, "host key" error | Run `plink pi@printserver.local` once and answer `y`, then try again |

More details (Wi-Fi only, updates, troubleshooting) are in the [README](../README.md).
