#!/bin/sh
# Run the whole suite on real Linux from a macOS/other checkout.
#
#   test/docker_linux.sh              # synthesized Cursor install (no secrets)
#   test/docker_linux.sh --live       # + copy the host's real Cursor session in,
#                                     #   so the sidecar talks to the live backend
#
# --live copies your Cursor auth token into a throwaway container (never into an
# image layer, and the mount is read-only). It's the only way to prove the Linux
# path end-to-end against the real backend; skip it if you'd rather not.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LIVE=${1:-}
MAC_CURSOR="$HOME/Library/Application Support/Cursor"
IMAGE=${NC_TEST_IMAGE:-alpine:3.22}

# Built as positional params, not a string: "Application Support" has a space in
# it, and an unquoted mount list splits on it.
set -- --rm -i -v "$ROOT:/src:ro"
if [ "$LIVE" = "--live" ]; then
  [ -d "$MAC_CURSOR" ] || { echo "no Cursor install at $MAC_CURSOR" >&2; exit 1; }
  set -- "$@" -v "$MAC_CURSOR:/cursor-src:ro"
fi

exec docker run "$@" -e LIVE="$LIVE" "$IMAGE" sh -es <<'CONTAINER'
set -eu
echo "=== $(cat /etc/os-release | sed -n 's/^PRETTY_NAME=//p') $(uname -m) ==="
apk add --no-cache neovim python3 ca-certificates >/dev/null

# uv is how users actually launch the sidecar, so prefer it; fall back to a
# preinstalled httpx if this image doesn't carry a uv package.
if apk add --no-cache uv >/dev/null 2>&1; then
  export UV_CACHE_DIR=/tmp/uv
  echo "launcher: uv"
else
  apk add --no-cache py3-httpx py3-h2 >/dev/null
  export NC_SIDECAR_CMD=python3
  echo "launcher: system python3 + py3-httpx (no uv package in image)"
fi

# Copy out of the read-only mount so pycache/uv writes and SQLite WAL work.
cp -r /src /w && cd /w

if [ "$LIVE" = "--live" ]; then
  mkdir -p /root/.config
  cp -r /cursor-src /root/.config/Cursor
  echo "using the host's real Cursor session at ~/.config/Cursor"
fi

echo; echo "--- path resolution ---";        python3 test/paths_spec.py
echo; echo "--- sidecar handshake ---";      python3 test/handshake_spec.py
echo; echo "--- headless tab flow ---";      nvim --headless -u NONE -c "luafile test/flow_spec.lua"

if [ "$LIVE" = "--live" ]; then
  echo; echo "--- live backend round-trip (real token, Linux paths) ---"
  { printf '%s\n' '{"id":1,"path":"demo.py","content":"def add(a, b):\n    return a + \n","line":1,"col":15,"language":"python"}'
    sleep 20
  } | ${NC_SIDECAR_CMD:-uv run --with httpx[http2]} sidecar.py 2>&1 | tail -1
fi
CONTAINER
