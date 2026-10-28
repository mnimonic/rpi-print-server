#!/bin/bash
# Release and deploy the APPLICATION (the welcome page and the scan page, in app/).
# Run on your computer (Linux, macOS, or Git Bash on Windows), not on the Pi.
#
#   ./deploy/deploy-app.sh                          test: deploy the working tree as it is now
#   ./deploy/deploy-app.sh v1.4.0 "What changed"    release: tag the commit as v1.4.0, build
#                                                   dist/rpi-print-server-v1.4.0.tar.gz, deploy it
#   ./deploy/deploy-app.sh dist/rpi-print-server-v1.4.0.tar.gz
#                                                   deploy a package built earlier (e.g. roll back)
# Options:
#   --dry-run     only show which files would change on the Pi (not with a new version)
#   --no-deploy   only build (and with a version, tag) the package; prints its path
#
# Set PI_HOST, PI_USER and PI_PASS first (see deploy/lib.sh), for example:
#   PI_HOST=raspberrypi.local PI_USER=pi PI_PASS=... ./deploy/deploy-app.sh --dry-run
#
# On the Pi the package's install.sh copies only the files that differ, restarts only the
# affected services and checks that they're running.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

usage() { sed -n '5,12p' "$0" >&2; exit 2; }
DRY_RUN="" NO_DEPLOY="" TAG="" MSG="" PKG=""
while [ $# -gt 0 ]; do
    case "$1" in
        -n|--dry-run) DRY_RUN="--dry-run" ;;
        --no-deploy) NO_DEPLOY=1 ;;
        -h|--help) usage ;;
        -*) usage ;;
        v[0-9]*) TAG=$1; if [ $# -gt 1 ] && [[ $2 != -* ]]; then MSG=$2; shift; fi ;;
        *) PKG=$1 ;;
    esac
    shift
done
[ -z "$TAG" ] || [ -z "$PKG" ] || die "give either a new version or a package, not both"
[ -z "$TAG" ] || [ -z "$DRY_RUN" ] || die "--dry-run can't be combined with a new version; dry-run the working tree first (no arguments)"

# 1. Build (and for a release, tag) the package
if [ -n "$TAG" ]; then
    PKG="$(build_app_package "$TAG")"
    git tag -a "$TAG" -m "${MSG:-$TAG}"
    echo "Tagged $TAG. Publish it with: git push --follow-tags" >&2
elif [ -z "$PKG" ]; then
    PKG="$(build_app_package)"
fi
[ -z "$NO_DEPLOY" ] || { echo "$PKG"; exit 0; }

# 2. Upload it to the Pi and run its installer
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
unpack_app_package "$PKG" "$STAGE"
echo "Deploying $(cat "$STAGE/VERSION") to $PI_USER@$PI_HOST"
upload_and_run "$STAGE" "bash install.sh $DRY_RUN"
[ -n "$DRY_RUN" ] || echo "Done. Welcome page: http://$PI_HOST/  Scan page: http://$PI_HOST:8080/"
