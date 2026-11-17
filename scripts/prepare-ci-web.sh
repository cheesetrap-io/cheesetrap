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
  npx --no-install playwright install --with-deps chromium
fi
