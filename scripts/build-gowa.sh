#!/bin/sh
# Builds GoWA (the fork with the wa-mac changes, branch wa-companion) into the
# companion data folder.
#
#   GOWA_SRC    src/ folder of a local clone of the fork; when set, that code is
#               used as is and nothing is downloaded
#   GOWA_REPO   repository to clone (default: the public fork)
#   GOWA_REF    branch or tag to clone (default: wa-companion)
#   WA_GOWA_DIR data folder (default ~/.local/share/wa-mac/gowa)
#
# Needs git and Go (https://go.dev/dl/).
set -eu
REPO="${GOWA_REPO:-https://github.com/jxnxts/go-whatsapp-web-multidevice.git}"
REF="${GOWA_REF:-wa-companion}"
DATA="${WA_GOWA_DIR:-$HOME/.local/share/wa-mac/gowa}"

command -v go >/dev/null 2>&1 || { echo "error: Go not found (brew install go)" >&2; exit 1; }

if [ -n "${GOWA_SRC:-}" ]; then
  SRC="$GOWA_SRC"
else
  command -v git >/dev/null 2>&1 || { echo "error: git not found" >&2; exit 1; }
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  trap 'exit 1' INT TERM
  echo "cloning $REPO ($REF) ..." >&2
  git clone --quiet --depth 1 --branch "$REF" "$REPO" "$TMP/gowa"
  SRC="$TMP/gowa/src"
fi

mkdir -p "$DATA/bin"
chmod 700 "$(dirname "$DATA")" "$DATA" "$DATA/bin"
cd "$SRC"
if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
  echo "building $(git -C "$SRC" rev-parse --abbrev-ref HEAD)@$(git -C "$SRC" rev-parse --short HEAD) ..." >&2
else
  echo "building $SRC ..." >&2
fi
go build -trimpath -o "$DATA/bin/gowa.new" .
mv "$DATA/bin/gowa.new" "$DATA/bin/gowa"
chmod 700 "$DATA/bin/gowa"
echo "ok: $DATA/bin/gowa" >&2
echo "start or restart the service with: wa companion service start" >&2
