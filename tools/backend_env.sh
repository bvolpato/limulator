#!/usr/bin/env bash
# Source this file for task-scoped native build and runtime paths.
export LIMULATOR_HOME=${LIMULATOR_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/limulator}
export LIMULATOR_CACHE=${LIMULATOR_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/limulator}
export LIMULATOR_BACKEND=${LIMULATOR_BACKEND:-$LIMULATOR_HOME/backends/vphone-linux}
export LIMULATOR_PREFIX=${LIMULATOR_PREFIX:-$LIMULATOR_HOME/tools}
export LIMULATOR_JOBS=${LIMULATOR_JOBS:-6}
export PREFIX=$LIMULATOR_PREFIX
export PATH=$LIMULATOR_PREFIX/bin:$HOME/.cargo/bin:/usr/bin:$PATH
export PKG_CONFIG=/usr/bin/pkg-config
export PKG_CONFIG_PATH=$LIMULATOR_PREFIX/lib/pkgconfig:$LIMULATOR_PREFIX/lib64/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}
export LD_LIBRARY_PATH=$LIMULATOR_PREFIX/lib:$LIMULATOR_PREFIX/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export CPPFLAGS="-I$LIMULATOR_PREFIX/include${CPPFLAGS:+ $CPPFLAGS}"
export LDFLAGS="-L$LIMULATOR_PREFIX/lib -Wl,-rpath,$LIMULATOR_PREFIX/lib${LDFLAGS:+ $LDFLAGS}"
export CMAKE_PREFIX_PATH=$LIMULATOR_PREFIX${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}
export CARGO_BUILD_JOBS=$LIMULATOR_JOBS
export CMAKE_BUILD_PARALLEL_LEVEL=$LIMULATOR_JOBS
export MAKEFLAGS="-j$LIMULATOR_JOBS"
export SWIFTLY_HOME_DIR=$LIMULATOR_HOME/swiftly
export SWIFTLY_TOOLCHAINS_DIR=$SWIFTLY_HOME_DIR/toolchains
export VSETUP_SRC=${VSETUP_SRC:-$LIMULATOR_BACKEND}
release_base=${LIMULATOR_RELEASE_BASE:-${VSETUP_RELEASE:-$LIMULATOR_HOME/releases/vphone}}
case $release_base in
  \~) release_base=$HOME ;;
  \~/*) release_base=$HOME/${release_base:2} ;;
esac
release_base=$(realpath -m -- "$release_base")
export LIMULATOR_RELEASE_BASE="$release_base"
release_tools=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
release_lock=$release_tools/../third_party.lock.json
export VSETUP_RELEASE="$release_base"
if command -v jq >/dev/null && [[ -f $release_lock ]]; then
  if release_version=$(jq -er .release.version "$release_lock" 2>/dev/null) &&
     release_sha256=$(jq -er .release.sha256 "$release_lock" 2>/dev/null); then
    release_sha256=${release_sha256,,}
    if [[ $release_version =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && $release_sha256 =~ ^[[:xdigit:]]{64}$ ]]; then
      export VSETUP_RELEASE="$release_base/$release_version-$release_sha256"
    fi
  fi
fi
export VSETUP_DEVICES=${VSETUP_DEVICES:-$LIMULATOR_HOME/vms}
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  export -p | LC_ALL=C sort | grep -E ' (LIMULATOR_|PREFIX=|PATH=|PKG_CONFIG=|PKG_CONFIG_PATH=|LD_LIBRARY_PATH=|CPPFLAGS=|LDFLAGS=|CMAKE_PREFIX_PATH=|CARGO_BUILD_JOBS=|CMAKE_BUILD_PARALLEL_LEVEL=|MAKEFLAGS=|SWIFTLY_|VSETUP_)'
fi
