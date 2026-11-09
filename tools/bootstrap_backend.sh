#!/usr/bin/env bash
# Build the real x86_64 Linux backend without upstream's persistent host changes.
set -Eeuo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR
# shellcheck source=backend_env.sh
source "$here/backend_env.sh"
lock=$here/../third_party.lock.json
phase=${1:-all}
if [[ $phase == --help || $phase == -h ]]; then
  cat <<'EOF'
Usage: tools/bootstrap_backend.sh [PHASE]
Phases: deps, sources, apple-usb, build-tools, crypto, swift, signing, gpu, qemu, cli,
        usbmuxd, apfs, release, all
all builds the backend. Run deps separately to install distro build dependencies.
No phase starts a VM, changes udev/service settings, or installs a kernel module.
State: ~/.local/share/limulator; downloads and dependency sources: ~/.cache/limulator.
LIMULATOR_HOME, LIMULATOR_CACHE, LIMULATOR_BACKEND, LIMULATOR_PREFIX and
LIMULATOR_JOBS override these paths and the build parallelism (maximum 6).
EOF
  exit
fi
[[ $# -le 1 ]] || { echo 'Only one phase is accepted' >&2; exit 2; }
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || { echo 'This backend build requires x86_64 Linux' >&2; exit 2; }
[[ $LIMULATOR_JOBS =~ ^[1-6]$ ]] || { echo 'LIMULATOR_JOBS must be between 1 and 6' >&2; exit 2; }
if [[ $phase != deps ]]; then
  command -v jq >/dev/null || { echo 'Install jq before bootstrapping' >&2; exit 1; }
fi
mkdir -p "$LIMULATOR_HOME" "$LIMULATOR_CACHE/downloads" "$LIMULATOR_CACHE/sources"
fetch_apfs_checkout() {
  local src=$1 repository=$2 revision=$3
  if [[ ! -e $src ]]; then git clone "$repository" "$src"; fi
  [[ -d $src/.git || -f $src/.git ]] || { echo "Not a Git checkout: $src" >&2; return 1; }
  if [[ $(git -C "$src" rev-parse HEAD) != "$revision" ]]; then
    if ! git -C "$src" diff --quiet --ignore-submodules=all || ! git -C "$src" diff --cached --quiet --ignore-submodules=all; then
      echo "Refusing to alter modified checkout: $src" >&2; return 1
    fi
    git -C "$src" cat-file -e "$revision^{commit}" 2>/dev/null || git -C "$src" fetch origin "$revision"
    git -C "$src" checkout --detach "$revision"
  fi
}
download() {
  local url=$1 destination=$2 checksum=$3
  if [[ ! -f $destination ]]; then
    curl -fL --retry 3 --continue-at - -o "$destination.partial" "$url"
    printf '%s  %s\n' "$checksum" "$destination.partial" | sha256sum --check --status
    mv "$destination.partial" "$destination"
  fi
  printf '%s  %s\n' "$checksum" "$destination" | sha256sum --check
}
contained() {
  local available swap
  available=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
  swap=$(awk '/^SwapFree:/{print $2}' /proc/meminfo)
  if (( available < 32 * 1024 * 1024 || swap < 4 * 1024 * 1024 )); then
    echo 'Build postponed: need 32 GiB MemAvailable and 4 GiB SwapFree' >&2
    return 1
  fi
  systemd-run --user --scope -p MemoryHigh=32G -p MemoryMax=40G -p MemorySwapMax=4G -- "$@"
}
build_deps() {
  sudo apt-get install -y --no-install-recommends \
    git gcc g++ clang lld binutils make cmake ninja-build pkg-config autoconf automake libtool-bin \
    python3 python3-pil python3-setuptools rsync unzip curl zstd jq \
    libglib2.0-dev libpixman-1-dev liblzfse-dev libslirp-dev libssh-dev libzstd-dev nettle-dev libgnutls28-dev libgtk-3-dev libsdl2-dev \
    libbz2-dev libx11-dev libxss-dev libxkbcommon-dev libwayland-dev wayland-protocols libvulkan-dev vulkan-tools \
    libdecor-0-dev libdrm-dev libxrandr-dev libxcursor-dev libxi-dev libxfixes-dev libxtst-dev \
    socat hwdata sshfs fuse3 7zip bc flex bison libelf-dev device-tree-compiler \
    binutils-gold zip gnupg2 libpython3-dev libxml2-dev libncurses-dev libz3-dev libedit-dev libsqlite3-dev \
    libusb-1.0-0-dev libssl-dev libcurl4-openssl-dev libarchive-dev "linux-headers-$(uname -r)"
}
build_sources() {
  command -v uv >/dev/null || { echo 'uv is required to prepare pinned backend sources' >&2; return 1; }
  uv run --script "$here/sync_backend_sources.py" --lock "$lock" --backend "$LIMULATOR_BACKEND"
}
verify_sources() {
  uv run --script "$here/sync_backend_sources.py" --lock "$lock" --backend "$LIMULATOR_BACKEND" --check
}
build_apple_usb() { "$here/build_apple_usb.sh"; }
build_tools() {
  local archive
  command -v uv >/dev/null || { echo 'uv is required to install private Meson' >&2; return 1; }
  archive=$LIMULATOR_CACHE/downloads/meson-$(jq -er .meson.version "$lock")-py3-none-any.whl
  download "$(jq -er .meson.url "$lock")" "$archive" "$(jq -er .meson.sha256 "$lock")"
  if [[ ! -x $LIMULATOR_PREFIX/python/bin/python ]]; then
    uv venv --python /usr/bin/python3 "$LIMULATOR_PREFIX/python"
  fi
  uv pip install --python "$LIMULATOR_PREFIX/python/bin/python" --no-deps "$archive"
  mkdir -p "$LIMULATOR_PREFIX/bin"
  ln -sfn "$LIMULATOR_PREFIX/python/bin/meson" "$LIMULATOR_PREFIX/bin/meson"
  "$LIMULATOR_PREFIX/bin/meson" --version
}
build_crypto() {
  local version archive src
  version=$(jq -er .nettle.version "$lock")
  src=$LIMULATOR_CACHE/sources/nettle-$version
  if [[ -r $LIMULATOR_PREFIX/.receipts/nettle && $(cat "$LIMULATOR_PREFIX/.receipts/nettle") == "$version" ]] &&
     [[ $(pkg-config --modversion nettle) == "$version" ]]; then
    return 0
  fi
  archive=$LIMULATOR_CACHE/downloads/nettle-$version.tar.gz
  download "$(jq -er .nettle.url "$lock")" "$archive" "$(jq -er .nettle.sha256 "$lock")"
  if [[ ! -d $src ]]; then tar -xzf "$archive" -C "$LIMULATOR_CACHE/sources"; fi
  (
    cd "$src"
    ./configure --prefix="$LIMULATOR_PREFIX" --libdir="$LIMULATOR_PREFIX/lib" --disable-documentation
    make -j"$LIMULATOR_JOBS"
    make -j"$LIMULATOR_JOBS" check
    make install
  )
  mkdir -p "$LIMULATOR_PREFIX/.receipts"
  printf '%s\n' "$version" > "$LIMULATOR_PREFIX/.receipts/nettle"
}
build_swift() {
  local version archive toolchain
  version=$(jq -er .swift.version "$lock")
  archive=$LIMULATOR_CACHE/downloads/swift-$version-RELEASE-ubuntu24.04.tar.gz
  toolchain=$SWIFTLY_TOOLCHAINS_DIR/$version
  if [[ ! -x $toolchain/usr/bin/swift ]]; then
    download "$(jq -er .swift.url "$lock")" "$archive" "$(jq -er .swift.sha256 "$lock")"
    mkdir -p "$toolchain"
    tar -xzf "$archive" --strip-components=1 -C "$toolchain"
  fi
  "$toolchain/usr/bin/swift" --version
}
build_signing() {
  local version archive folder
  version=$(jq -er .rcodesign.version "$lock")
  folder=apple-codesign-$version-x86_64-unknown-linux-musl
  archive=$LIMULATOR_CACHE/downloads/$folder.tar.gz
  download "$(jq -er .rcodesign.url "$lock")" "$archive" "$(jq -er .rcodesign.sha256 "$lock")"
  mkdir -p "$LIMULATOR_PREFIX/bin" "$LIMULATOR_PREFIX/share/doc/rcodesign"
  tar -xzf "$archive" --strip-components=1 -C "$LIMULATOR_PREFIX/bin" "$folder/rcodesign"
  tar -xzf "$archive" --strip-components=1 -C "$LIMULATOR_PREFIX/share/doc/rcodesign" "$folder/COPYING"
  "$LIMULATOR_PREFIX/bin/rcodesign" --version
}
build_gpu() { verify_sources; contained make -C "$LIMULATOR_BACKEND/reims-vgpu"; }
build_qemu() {
  verify_sources
  build_crypto
  local pkgcfg=$LIMULATOR_BACKEND/reims-vgpu/target/dist-vulkan/lib/pkgconfig
  export PKG_CONFIG_PATH=$pkgcfg:$PKG_CONFIG_PATH
  mkdir -p "$LIMULATOR_BACKEND/qemu/build"
  (
    cd "$LIMULATOR_BACKEND/qemu/build"
    ../configure --target-list=aarch64-softmmu --enable-pvg --enable-lzfse \
      --enable-slirp --enable-libssh --enable-zstd --enable-nettle --enable-gnutls \
      --enable-gtk --enable-sdl --disable-werror --disable-qom-cast-debug
    contained ninja -j"$LIMULATOR_JOBS" qemu-system-aarch64
  )
}
build_cli() {
  local src
  src=$LIMULATOR_BACKEND/cpu-tree/vphone-cli-$(jq -er .vphone_cli.version "$lock")
  build_sources
  contained env VPHONE_CLI="$src" "$LIMULATOR_BACKEND/linux-create/build.sh" \
    --configuration release --jobs "$LIMULATOR_JOBS" -Xcc "-I$LIMULATOR_PREFIX/include" \
    -Xlinker "-L$LIMULATOR_PREFIX/lib" -Xlinker -rpath -Xlinker "$LIMULATOR_PREFIX/lib"
}
build_usbmuxd() { verify_sources; "$LIMULATOR_BACKEND/tools/build-usbmuxd-32k.sh"; }
build_apfs() {
  verify_sources
  local src=$LIMULATOR_BACKEND/.build/linux-apfs-rw revision
  revision=$(jq -er .apfs.revision "$lock")
  mkdir -p "$(dirname "$src")"
  # Keep the upstream backend's APFS compatibility patch until that dependency has a fork pin.
  fetch_apfs_checkout "$src" "$(jq -er .apfs.repository "$lock")" "$revision"
  if git -C "$src" apply --check "$LIMULATOR_BACKEND/patches/linux-apfs-rw-ios27.patch" 2>/dev/null; then
    git -C "$src" apply "$LIMULATOR_BACKEND/patches/linux-apfs-rw-ios27.patch"
  else
    git -C "$src" apply --reverse --check "$LIMULATOR_BACKEND/patches/linux-apfs-rw-ios27.patch"
  fi
  [[ -e /lib/modules/$(uname -r)/build ]] || { echo "Missing headers for $(uname -r)" >&2; return 1; }
  if [[ -f $src/apfs.ko && $(modinfo -F vermagic "$src/apfs.ko" | cut -d' ' -f1) != "$(uname -r)" ]]; then
    make -C "$src" clean
  fi
  contained make -j"$LIMULATOR_JOBS" -C "$src"
  modinfo -F vermagic "$src/apfs.ko"
}
build_release() {
  verify_sources
  local version resource_helper
  version=$(jq -er .release.version "$lock")
  resource_helper=$here/../src/limulator/release_resources.py
  [[ -f $resource_helper ]] || resource_helper=$here/../release_resources.py
  VSETUP_RELEASE=$(uv run --script "$resource_helper" path --lock "$lock" --data "$LIMULATOR_HOME")
  export VSETUP_RELEASE
  mkdir -p "$VSETUP_RELEASE"
  download "$(jq -er .release.url "$lock")" "$VSETUP_RELEASE/VPhone-$version.zip" "$(jq -er .release.sha256 "$lock")"
  uv run --script "$resource_helper" install --lock "$lock" --data "$LIMULATOR_HOME" --backend "$LIMULATOR_BACKEND"
}
case $phase in
  all) for step in sources apple_usb tools crypto swift signing gpu qemu cli usbmuxd apfs release; do "build_$step"; done ;;
  apple-usb) build_apple_usb ;;
  build-tools) build_tools ;;
  deps|sources|crypto|swift|signing|gpu|qemu|cli|usbmuxd|apfs|release) "build_$phase" ;;
  *) printf 'Unknown phase: %s\n' "$phase" >&2; exit 2 ;;
esac
