#!/bin/bash
# Deploy changes from this repository to an existing Pi installation.
# Run on your computer (Linux, macOS, or Git Bash on Windows), not on the Pi:
#   ./scripts/deploy.sh             copy changed files, restart what changed, verify
#   ./scripts/deploy.sh --dry-run   only show what would change
#
# It updates the web pages (welcome, scanweb), their systemd units, the Avahi records and
# hdmi-off.service. First-time setup is still done with scripts 01-08 on the Pi (see README).

# ---------------------------------------------------------------- your Pi
# Edit these, or set them as environment variables (PI_HOST=... ./scripts/deploy.sh).
# The repository is public: never commit a real password. Leave PI_PASS empty to use
# SSH keys; sudo will then ask for the password in the terminal.
PI_HOST="${PI_HOST:-192.168.1.50}"
PI_USER="${PI_USER:-pi}"
PI_PASS="${PI_PASS:-}"
# ----------------------------------------------------------------

set -euo pipefail
cd "$(dirname "$0")/.."

DRY_RUN=0
case "${1:-}" in
    "") ;;
    -n|--dry-run) DRY_RUN=1 ;;
    *) echo "Usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

# Files that get deployed (repo paths); apply.sh below maps them onto the Pi
FILES=(
    welcome/welcome.py welcome/welcome.service
    scanweb/scanweb.py scanweb/scanweb.service
    config/hdmi-off.service
    config/avahi/printserver-web.service config/avahi/printserver-scan.service
)

# ---------------------------------------------------------------- SSH transport
# PuTTY's plink (Windows) or OpenSSH. With a password, OpenSSH needs sshpass.
PLINK=""
for p in plink "/c/Program Files/PuTTY/plink.exe"; do
    command -v "$p" >/dev/null 2>&1 && { PLINK="$p"; break; }
done
if [ -n "$PI_PASS" ] && [ -z "$PLINK" ] && ! command -v sshpass >/dev/null 2>&1; then
    echo "PI_PASS is set, but neither plink nor sshpass is installed." >&2
    echo "Install one of them, or leave PI_PASS empty and use SSH keys." >&2
    exit 1
fi

remote() {   # remote <command>: runs on the Pi with this script's stdin
    if [ -n "$PLINK" ] && [ -n "$PI_PASS" ]; then
        "$PLINK" -batch -ssh -pw "$PI_PASS" "$PI_USER@$PI_HOST" "$1"
    elif [ -n "$PI_PASS" ]; then
        SSHPASS="$PI_PASS" sshpass -e ssh -o StrictHostKeyChecking=accept-new "$PI_USER@$PI_HOST" "$1"
    else
        ssh -o StrictHostKeyChecking=accept-new "$PI_USER@$PI_HOST" "$1"
    fi
}

remote_sudo() {   # remote_sudo <script on the Pi> [args]
    if [ -n "$PI_PASS" ]; then
        # sudo reads the password from stdin; the script itself doesn't read stdin
        printf '%s\n' "$PI_PASS" | remote "sudo -S -p '' bash $*"
    else
        # Needs a terminal so sudo can prompt
        ssh -t "$PI_USER@$PI_HOST" "sudo bash $*"
    fi
}

# ---------------------------------------------------------------- stage the files
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
for f in "${FILES[@]}"; do
    [ -f "$f" ] || { echo "Missing $f (run from a complete checkout)" >&2; exit 1; }
    mkdir -p "$STAGE/$(dirname "$f")"
    cp "$f" "$STAGE/$f"
done

# Runs on the Pi as root
cat > "$STAGE/apply.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
DRY_RUN="${1:-0}"
find . -type f -exec sed -i 's/\r$//' {} +   # in case of a CRLF checkout

RESTART=()
RELOAD=0
put() {   # put <repo file> <destination> <mode> [service to restart]
    local src=$1 dest=$2 mode=$3 svc=${4:-}
    if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
        echo "  unchanged  $dest"
        return 0
    fi
    echo "  update     $dest"
    if [ "$DRY_RUN" = 1 ]; then return 0; fi
    install -D -m "$mode" "$src" "$dest"
    if [[ $dest == /etc/systemd/system/* ]]; then RELOAD=1; fi
    if [ -n "$svc" ]; then RESTART+=("$svc"); fi
}
installed() {   # a component is only updated if its setup script was run before
    if [ -f "/etc/systemd/system/$1.service" ]; then return 0; fi
    echo "  skipped    $1 (not installed; run $2 on the Pi first)"
    return 1
}

echo "Changes on $(hostname):"
if installed welcome scripts/07-install-welcome.sh; then
    put welcome/welcome.py /usr/local/bin/welcome.py 755 welcome
    put welcome/welcome.service /etc/systemd/system/welcome.service 644 welcome
fi
if installed scanweb scripts/06-install-scanweb.sh; then
    put scanweb/scanweb.py /usr/local/bin/scanweb.py 755 scanweb
    put scanweb/scanweb.service /etc/systemd/system/scanweb.service 644 scanweb
fi
if installed hdmi-off scripts/03-tune-headless.sh; then
    put config/hdmi-off.service /etc/systemd/system/hdmi-off.service 644   # takes effect at boot
fi
if [ -d /etc/avahi/services ]; then
    # Avahi notices new or changed files by itself
    put config/avahi/printserver-web.service /etc/avahi/services/printserver-web.service 644
    put config/avahi/printserver-scan.service /etc/avahi/services/printserver-scan.service 644
else
    echo "  skipped    Avahi records (avahi-daemon not installed; run scripts/01-print-server.sh)"
fi

if [ "$DRY_RUN" = 1 ]; then
    echo "Dry run: nothing was changed."
    exit 0
fi
if [ "$RELOAD" = 1 ]; then systemctl daemon-reload; fi
if [ ${#RESTART[@]} -eq 0 ]; then
    echo "Nothing to restart."
    exit 0
fi
SERVICES=$(printf '%s\n' "${RESTART[@]}" | sort -u)
for s in $SERVICES; do
    echo "Restarting $s"
    systemctl restart "$s"
done
sleep 4   # Pi 1: give Python a moment to start
FAILED=0
for s in $SERVICES; do
    if systemctl is-active --quiet "$s"; then
        echo "  $s is running"
    else
        echo "  $s FAILED to start:"
        journalctl -u "$s" -n 15 --no-pager
        FAILED=1
    fi
done
exit $FAILED
EOF

# ---------------------------------------------------------------- upload and apply
echo "Deploying to $PI_USER@$PI_HOST"
REMOTE_DIR=/tmp/rpi-deploy
tar -C "$STAGE" -cf - . | remote "rm -rf $REMOTE_DIR && mkdir -p $REMOTE_DIR && tar -xf - -C $REMOTE_DIR" || {
    echo "Upload failed. Check PI_HOST/PI_USER/PI_PASS. With plink, connect once by hand" >&2
    echo "(plink $PI_USER@$PI_HOST) to accept the Pi's host key." >&2
    exit 1
}
STATUS=0
remote_sudo "$REMOTE_DIR/apply.sh $DRY_RUN" || STATUS=$?
remote "rm -rf $REMOTE_DIR" </dev/null || true

if [ $STATUS -eq 0 ] && [ $DRY_RUN = 0 ]; then
    echo "Done. Welcome page: http://$PI_HOST/  Scan page: http://$PI_HOST:8080/"
fi
exit $STATUS
