#!/bin/sh
# Called with a fresh private acceptance directory on the Linux AMD64 CI runner.
set -eu
umask 077
out=$1
version=$2
sha256=$3
scratch=$(mktemp -d "$out/sqlc-install.XXXXXXXX")
trap 'rm -rf -- "$scratch"' EXIT
url="https://github.com/sqlc-dev/sqlc/releases/download/v$version/sqlc_${version}_linux_amd64.tar.gz"
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' "$url" > "$scratch/release.tgz"
(cd "$scratch" && printf '%s  release.tgz\n' "$sha256" | sha256sum --check --strict -)
tar -xzOf "$scratch/release.tgz" sqlc > "$scratch/sqlc"
chmod 700 "$scratch/sqlc"
reported=$("$scratch/sqlc" version)
if [ "$reported" != "v$version" ]; then
    echo 'Unexpected sqlc release version' >&2
    exit 1
fi
mv "$scratch/sqlc" "$out/sqlc"
