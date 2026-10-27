#!/bin/bash
# Build a release package that scripts/deploy.sh installs on the Pi.
# Run on your computer (Linux, macOS, or Git Bash on Windows):
#   ./scripts/release.sh v1.2.0 ["what changed"]   package the current commit and tag it v1.2.0
#   ./scripts/release.sh                           package the working tree as a test snapshot
#
# Output: dist/rpi-print-server-<version>.tar.gz containing the deployable files (welcome.py
# with the version stamped in), install.sh and VERSION. Messages go to stderr; the package
# path is printed on stdout, so other scripts can use PKG=$(./scripts/release.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

# Everything that goes onto the Pi; scripts/install-release.sh maps each file to its place
FILES=(
    welcome/welcome.py welcome/welcome.service
    scanweb/scanweb.py scanweb/scanweb.service
    config/hdmi-off.service
    config/avahi/printserver-web.service config/avahi/printserver-scan.service
)

die() { echo "release: $*" >&2; exit 1; }

TAG="${1:-}"
if [ -n "$TAG" ]; then
    [[ $TAG =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "the version must look like v1.2.0"
    ! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || die "tag $TAG already exists"
    [ -z "$(git status --porcelain)" ] || die "commit or stash your changes first (a release is built from a commit)"
    VERSION="$TAG"
else
    # e.g. v1.1.0-3-gabc1234-dirty: 3 commits after v1.1.0, plus uncommitted changes
    VERSION="$(git describe --tags --always --dirty 2>/dev/null || echo dev)"
fi

NAME="rpi-print-server-$VERSION"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
OUT="$STAGE/$NAME"
mkdir -p "$OUT"

if [ -n "$TAG" ]; then
    # Exactly what is committed
    git archive HEAD "${FILES[@]}" scripts/install-release.sh | tar -x -C "$OUT"
else
    for f in "${FILES[@]}" scripts/install-release.sh; do
        [ -f "$f" ] || die "missing $f"
        mkdir -p "$OUT/$(dirname "$f")"
        cp "$f" "$OUT/$f"
    done
fi
mv "$OUT/scripts/install-release.sh" "$OUT/install.sh"
rmdir "$OUT/scripts"
chmod 755 "$OUT/install.sh" "$OUT/welcome/welcome.py" "$OUT/scanweb/scanweb.py"
echo "$VERSION" > "$OUT/VERSION"

sed "s/^VERSION = \"dev\"/VERSION = \"$VERSION\"/" "$OUT/welcome/welcome.py" > "$STAGE/welcome.py"
grep -q "^VERSION = \"$VERSION\"$" "$STAGE/welcome.py" || die "could not stamp the version into welcome.py"
cat "$STAGE/welcome.py" > "$OUT/welcome/welcome.py"

mkdir -p dist
tar -czf "dist/$NAME.tar.gz" -C "$STAGE" "$NAME"

if [ -n "$TAG" ]; then
    git tag -a "$TAG" -m "${2:-$TAG}"
    echo "Tagged $TAG. Publish it with: git push --follow-tags" >&2
fi
echo "Built dist/$NAME.tar.gz" >&2
echo "dist/$NAME.tar.gz"
