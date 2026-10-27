#!/bin/bash
# Deploy a release package to an existing Pi installation.
# Run on your computer (Linux, macOS, or Git Bash on Windows), not on the Pi:
#   ./scripts/deploy.sh dist/rpi-print-server-v1.2.0.tar.gz   install that release
#   ./scripts/deploy.sh                                       build a snapshot of the working
#                                                             tree (scripts/release.sh), install it
#   ./scripts/deploy.sh --dry-run [package]                   only show what would change
#
# The package's install.sh copies only the changed files, restarts only the affected services
# and checks they are running. First-time setup is done with scripts 01-07 on the Pi.

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

DRY_RUN=""
PKG=""
for arg in "$@"; do
    case "$arg" in
        -n|--dry-run) DRY_RUN="--dry-run" ;;
        -*) echo "Usage: $0 [--dry-run] [package.tar.gz]" >&2; exit 2 ;;
        *) PKG="$arg" ;;
    esac
done
if [ -z "$PKG" ]; then
    PKG="$(./scripts/release.sh)"
fi
[ -f "$PKG" ] || { echo "No such package: $PKG" >&2; exit 1; }
NAME="$(basename "$PKG" .tar.gz)"
VERSION="$(tar -xzOf "$PKG" "$NAME/VERSION")"

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

remote_sudo() {   # remote_sudo <command>
    if [ -n "$PI_PASS" ]; then
        # sudo reads the password from stdin; install.sh itself doesn't read stdin
        printf '%s\n' "$PI_PASS" | remote "sudo -S -p '' $1"
    else
        # Needs a terminal so sudo can prompt
        ssh -t "$PI_USER@$PI_HOST" "sudo $1"
    fi
}

# ---------------------------------------------------------------- upload and install
echo "Deploying $VERSION to $PI_USER@$PI_HOST"
REMOTE_DIR=/tmp/rpi-deploy
remote "rm -rf $REMOTE_DIR && mkdir -p $REMOTE_DIR && tar -xzf - -C $REMOTE_DIR --strip-components=1" < "$PKG" || {
    echo "Upload failed. Check PI_HOST/PI_USER/PI_PASS. With plink, connect once by hand" >&2
    echo "(plink $PI_USER@$PI_HOST) to accept the Pi's host key." >&2
    exit 1
}
STATUS=0
remote_sudo "bash $REMOTE_DIR/install.sh $DRY_RUN" || STATUS=$?
remote "rm -rf $REMOTE_DIR" </dev/null || true

if [ $STATUS -eq 0 ] && [ -z "$DRY_RUN" ]; then
    echo "Done. Welcome page: http://$PI_HOST/  Scan page: http://$PI_HOST:8080/"
fi
exit $STATUS
