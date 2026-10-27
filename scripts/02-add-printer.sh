#!/bin/bash
# Add and share an HP DeskJet 1510-series printer (DeskJet 1510/1512/1515...) on USB.
# Run on the Pi as root:  sudo ./02-add-printer.sh
# Works even before the printer is plugged in; jobs wait until it is connected.
set -euo pipefail
QUEUE="DeskJet_1515"
PAPER="${PAPER:-A4}"

# Matches any "Deskjet 1510 series" on USB. To pin one specific printer, use the
# hp: URI with its serial instead (see `sudo lpinfo -v` while it is plugged in).
URI='usb://HP/Deskjet%201510%20series'

lpadmin -p "$QUEUE" -E \
  -v "$URI" \
  -m drv:///hpcups.drv/hp-deskjet_1510_series.ppd \
  -D 'HP DeskJet 1515' -L 'Raspberry Pi print server' \
  -o printer-is-shared=true -o PageSize="$PAPER" -o printer-error-policy=retry-job
lpadmin -d "$QUEUE"
cupsenable "$QUEUE"
cupsaccept "$QUEUE"
lpstat -t
