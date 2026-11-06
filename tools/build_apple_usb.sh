#!/usr/bin/env bash
# Noble's older libraries remain installed. Build restore dependencies privately.
set -Eeuo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR
# shellcheck source=backend_env.sh
source "$here/backend_env.sh"
lock=$here/../third_party.lock.json
command -v jq >/dev/null || { echo 'jq is required' >&2; exit 1; }
mkdir -p "$LIMULATOR_CACHE/sources" "$LIMULATOR_PREFIX/.receipts"
for name in libplist libimobiledevice-glue libtatsu libusbmuxd libimobiledevice libirecovery; do
  revision=$(jq -er --arg name "$name" '.apple_usb[$name].revision' "$lock")
  repository=$(jq -er --arg name "$name" '.apple_usb[$name].repository' "$lock")
  src=$LIMULATOR_CACHE/sources/$name
  receipt=$LIMULATOR_PREFIX/.receipts/$name
  if [[ -r $receipt && $(cat "$receipt") == "$revision" ]]; then
    printf 'Already installed %s (%s)\n' "$name" "$revision"
    continue
  fi
  if [[ ! -d $src/.git ]]; then git clone "$repository" "$src"; fi
  if [[ $(git -C "$src" rev-parse HEAD) != "$revision" ]]; then
    if ! git -C "$src" diff --quiet || ! git -C "$src" diff --cached --quiet; then
      echo "Refusing to alter modified $src" >&2
      exit 1
    fi
    git -C "$src" cat-file -e "$revision^{commit}" 2>/dev/null || git -C "$src" fetch origin "$revision"
    git -C "$src" checkout --detach "$revision"
  fi
  flags=(--prefix="$LIMULATOR_PREFIX" --libdir="$LIMULATOR_PREFIX/lib")
  case $name in
    libplist) flags+=(--without-cython --without-tests) ;;
    libimobiledevice) flags+=(--without-cython --with-openssl --without-gnutls --without-mbedtls) ;;
    libirecovery) flags+=(--without-udev) ;;
  esac
  (
    cd "$src"
    ./autogen.sh "${flags[@]}"
    make -j"$LIMULATOR_JOBS"
    make install
  )
  printf '%s\n' "$revision" > "$receipt"
done
pkg-config --modversion libplist-2.0 libimobiledevice-glue-1.0 libtatsu-1.0 libusbmuxd-2.0 libimobiledevice-1.0 libirecovery-1.0
