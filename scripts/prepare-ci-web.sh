#!/bin/sh
set -eu
case "$#:$*" in
  0:) browser=true ;;
  1:--dependencies-only) browser=false ;;
  *) echo 'usage: prepare-ci-web.sh [--dependencies-only]' >&2; exit 2 ;;
esac
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$script_dir/../web"
npm ci --no-audit --no-fund
if "$browser"; then
  # The pinned Linux CLI simulates APT installation and succeeds only if no
  # packages are missing. Unknown output/platforms retain the full installer.
  if readiness=$(npx --no-install playwright install-deps --dry-run chromium 2>&1) &&
      [ "$readiness" = 'All system dependencies are installed.' ]; then
    npx --no-install playwright install --only-shell chromium
  else
    npx --no-install playwright install --with-deps --only-shell chromium
  fi
fi
