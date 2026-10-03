#!/bin/sh
# apt-get must have already verified GPG signatures. Reject missing/extra/drifting indices.
set -eu
expected=$1
lists=${2:-/var/lib/apt/lists}
actual=$(mktemp)
trap 'rm -f -- "$actual"' EXIT HUP INT TERM
for release in "$lists"/*_InRelease; do
  test -f "$release" || { echo 'No verified APT InRelease files' >&2; exit 1; }
  sha256sum "$release" | cut -d ' ' -f 1
done | LC_ALL=C sort > "$actual"
cmp "$expected" "$actual" || { echo 'APT snapshot fingerprint mismatch' >&2; exit 1; }
