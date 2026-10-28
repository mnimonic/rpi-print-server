#!/bin/bash
# Shared helpers for the deploy-*.sh scripts. Not run directly: they load it with
#   source "$(dirname "$0")/lib.sh"
#
# Connection settings come from environment variables, so no password is ever written into
# a tracked file (the repository is public):
#   PI_HOST   the Pi's address, e.g. rasp-printer.local or 192.168.1.50
#   PI_USER   the user you log in with over SSH (must be allowed to use sudo)
#   PI_PASS   that user's password. Leave it empty to use SSH keys; sudo then asks in the
#             terminal. With a password you need PuTTY's plink (Windows) or sshpass.

PI_HOST="${PI_HOST:-raspberrypi.local}"
PI_USER="${PI_USER:-pi}"
PI_PASS="${PI_PASS:-}"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

die() { echo "$(basename "$0"): $*" >&2; exit 1; }

# ---------------------------------------------------------------- SSH transport
PLINK=""
for p in plink "/c/Program Files/PuTTY/plink.exe"; do
    command -v "$p" >/dev/null 2>&1 && { PLINK="$p"; break; }
done
if [ -n "$PI_PASS" ] && [ -z "$PLINK" ] && ! command -v sshpass >/dev/null 2>&1; then
    die "PI_PASS is set, but neither plink nor sshpass is installed.
Install one of them, or leave PI_PASS empty and use SSH keys."
fi

remote() {   # remote <command>: runs <command> on the Pi, with this script's stdin
    if [ -n "$PLINK" ] && [ -n "$PI_PASS" ]; then
        "$PLINK" -batch -ssh -pw "$PI_PASS" "$PI_USER@$PI_HOST" "$1"
    elif [ -n "$PI_PASS" ]; then
        SSHPASS="$PI_PASS" sshpass -e ssh -o StrictHostKeyChecking=accept-new "$PI_USER@$PI_HOST" "$1"
    else
        ssh -o StrictHostKeyChecking=accept-new "$PI_USER@$PI_HOST" "$1"
    fi
}

remote_sudo() {   # remote_sudo <command>: runs <command> on the Pi as root
    if [ -n "$PI_PASS" ]; then
        # sudo reads the password from stdin; the installers themselves don't read stdin
        printf '%s\n' "$PI_PASS" | remote "sudo -S -p '' $1"
    else
        # Needs a terminal so sudo can prompt for the password
        ssh -t "$PI_USER@$PI_HOST" "sudo $1"
    fi
}

# upload_and_run <folder> <command>: copies <folder> to a temporary folder on the Pi, runs
# <command> inside it as root (e.g. "bash install.sh --dry-run"), then deletes it again.
upload_and_run() {
    local dir=$1 cmd=$2 tmp=/tmp/rpi-deploy-$$ status=0
    tar -czf - -C "$dir" . | remote "rm -rf $tmp && mkdir -p $tmp && tar -xzf - -C $tmp" || {
        echo "Upload to $PI_USER@$PI_HOST failed. Check PI_HOST, PI_USER and PI_PASS." >&2
        echo "With plink, connect once by hand (plink $PI_USER@$PI_HOST) to accept the Pi's host key." >&2
        return 1
    }
    remote_sudo "env -C $tmp $cmd" || status=$?
    remote "rm -rf $tmp" </dev/null || true
    return $status
}

# ---------------------------------------------------------------- application package
# build_app_package [vX.Y.Z]: builds dist/rpi-print-server-<version>.tar.gz from app/ and
# prints its path on stdout (messages go to stderr).
#   With a version: packages exactly what is committed, then tags the commit.
#   Without:        packages the working tree as a test snapshot, e.g. v1.3.0-2-gabc1234-dirty.
build_app_package() {
    local tag=${1:-} version name stage
    if [ -n "$tag" ]; then
        [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "the version must look like v1.2.0"
        ! git rev-parse -q --verify "refs/tags/$tag" >/dev/null || die "tag $tag already exists"
        [ -z "$(git status --porcelain)" ] || die "commit or stash your changes first (a release is built from a commit)"
        version=$tag
    else
        version="$(git describe --tags --always --dirty 2>/dev/null || echo dev)"
    fi
    name="rpi-print-server-$version"
    stage="$(mktemp -d)"
    mkdir -p "$stage/$name"
    if [ -n "$tag" ]; then
        git archive HEAD app | tar -x -C "$stage"
        mv "$stage/app"/* "$stage/$name/" && rmdir "$stage/app"
    else
        cp -r app/. "$stage/$name/"
    fi
    find "$stage/$name" -name __pycache__ -prune -exec rm -rf {} +
    echo "$version" > "$stage/$name/VERSION"
    mkdir -p dist
    tar -czf "dist/$name.tar.gz" -C "$stage" "$name"
    rm -rf "$stage"
    echo "Built dist/$name.tar.gz" >&2
    echo "dist/$name.tar.gz"
}

# unpack_app_package <package> <folder>: unpacks a package into <folder>
unpack_app_package() {
    local pkg=$1 out=$2
    [ -f "$pkg" ] || die "no such package: $pkg"
    mkdir -p "$out"
    tar -xzf "$pkg" -C "$out" --strip-components=1
    [ -f "$out/VERSION" ] && [ -f "$out/install.sh" ] || die "$pkg is not an application package"
}
