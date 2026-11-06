#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="$REPO_DIR/third_party.lock.json"

# shellcheck source=tools/backend_env.sh
source "$SCRIPT_DIR/backend_env.sh"

fail() {
  echo "error: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is missing: $1"
}

manifest_value() {
  jq -er "$1" "$MANIFEST"
}

require_command curl
require_command flock
require_command git
require_command install
require_command jq
require_command mktemp
require_command cp
require_command unlink
require_command sha256sum
require_command systemd-run

TOOL_TMP=""
TOOL_BACKUP_TMP=""
cleanup_xtool_install() {
  if [[ -n "$TOOL_TMP" && ( -e "$TOOL_TMP" || -L "$TOOL_TMP" ) ]]; then
    unlink -- "$TOOL_TMP" || true
  fi
  if [[ -n "$TOOL_BACKUP_TMP" && ( -e "$TOOL_BACKUP_TMP" || -L "$TOOL_BACKUP_TMP" ) ]]; then
    unlink -- "$TOOL_BACKUP_TMP" || true
  fi
}
trap cleanup_xtool_install EXIT

TOOL_VERSION="$(manifest_value '.xtool.version')"
TOOL_REPOSITORY="$(manifest_value '.xtool.repository')"
TOOL_REF="$(manifest_value '.xtool.ref')"
TOOL_REVISION="$(manifest_value '.xtool.revision')"
TOOL_ARCH="$(manifest_value '.xtool.architecture')"
BUILD_CONFIGURATION="$(manifest_value '.xtool.build.configuration')"
BUILD_PRODUCT="$(manifest_value '.xtool.build.product')"
BUILD_JOBS="${LIMULATOR_XTOOL_JOBS:-$(manifest_value '.xtool.build.jobs')}"
SWIFT_VERSION="$(manifest_value '.xtool.build.swift')"
SDK_VERSION="$(manifest_value '.xtool.sdk.version')"
SDK_CANONICAL_NAME="$(manifest_value '.xtool.sdk.canonical_name')"
SDK_ARCHIVE="$(manifest_value '.xtool.sdk.archive')"
SDK_SHA256="$(manifest_value '.xtool.sdk.sha256')"
SDK_BUNDLE_VERSION="$(manifest_value '.xtool.sdk.bundle_version')"

[[ "$(uname -s)" == Linux && "$(uname -m)" == "$TOOL_ARCH" ]] \
  || fail "xtool source pin requires Linux $TOOL_ARCH"
[[ "$BUILD_JOBS" =~ ^[1-4]$ ]] || fail "LIMULATOR_XTOOL_JOBS must be between 1 and 4"

SWIFT_BIN="$SWIFTLY_TOOLCHAINS_DIR/$SWIFT_VERSION/usr/bin"
SWIFT_LIB="$SWIFTLY_TOOLCHAINS_DIR/$SWIFT_VERSION/usr/lib/swift/linux"
[[ -x "$SWIFT_BIN/swift" ]] || fail "private Swift $SWIFT_VERSION is missing; run tools/bootstrap_backend.sh swift first"
[[ -d "$SWIFT_LIB" ]] || fail "private Swift runtime libraries are missing: $SWIFT_LIB"
export PATH="$SWIFT_BIN:$PATH"
export LD_LIBRARY_PATH="$LIMULATOR_PREFIX/lib:$LIMULATOR_PREFIX/lib64:$SWIFT_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
require_command swift

SWIFT_ACTUAL="$(swift --version | sed -n '1p')"
[[ "$SWIFT_ACTUAL" == "Swift version $SWIFT_VERSION "* ]] || fail "expected Swift $SWIFT_VERSION, found: $SWIFT_ACTUAL"

TOOL_CACHE="$LIMULATOR_CACHE/xtool/$TOOL_VERSION"
SOURCE_DIR="$TOOL_CACHE/source"
PRIVATE_TOOL_DIR="$LIMULATOR_PREFIX/libexec/limulator/xtool/$TOOL_REVISION"
PRIVATE_TOOL="$PRIVATE_TOOL_DIR/xtool"
XTOOL_HOME="$LIMULATOR_HOME/xtool"
SDK_CANDIDATE_HOME="$XTOOL_HOME/candidate-${TOOL_REVISION:0:7}"
SDK_CANDIDATE_CONFIG="$SDK_CANDIDATE_HOME/config"
SDK_CANDIDATE_BUNDLE="$SDK_CANDIDATE_CONFIG/swiftpm/swift-sdks/darwin.artifactbundle"
SDK_TARGET_CONFIG="$XTOOL_HOME/config"
SDK_TARGET_BUNDLE="$SDK_TARGET_CONFIG/swiftpm/swift-sdks/darwin.artifactbundle"
SDK_SOURCE="${LIMULATOR_XCODE_XIP:-$HOME/Downloads/$SDK_ARCHIVE}"

mkdir -p "$TOOL_CACHE" "$LIMULATOR_PREFIX/bin" "$LIMULATOR_PREFIX/libexec/limulator/xtool" "$XTOOL_HOME"
exec {BOOTSTRAP_LOCK_FD}>"$XTOOL_HOME/bootstrap.lock"
flock -x "$BOOTSTRAP_LOCK_FD"

check_memory_headroom() {
  local available swap
  available="$(awk '/^MemAvailable:/ { print $2 }' /proc/meminfo)"
  swap="$(awk '/^SwapFree:/ { print $2 }' /proc/meminfo)"
  [[ -n "$available" && -n "$swap" ]] || fail "could not read memory and swap headroom"
  (( available >= 32 * 1024 * 1024 && swap >= 4 * 1024 * 1024 )) \
    || fail "xtool setup needs 32 GiB MemAvailable and 4 GiB SwapFree; currently ${available} KiB and ${swap} KiB"
}

contained() {
  check_memory_headroom
  systemd-run --user --scope -p MemoryHigh=32G -p MemoryMax=40G -p MemorySwapMax=4G -- "$@"
}

if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  [[ ! -e "$SOURCE_DIR" ]] || fail "pinned xtool source path is not a Git checkout: $SOURCE_DIR"
  SOURCE_STAGE="$(mktemp -d "$TOOL_CACHE/source-stage.XXXXXX")"
  git clone --filter=blob:none --branch "$TOOL_REF" --single-branch -- "$TOOL_REPOSITORY" "$SOURCE_STAGE"
  git -C "$SOURCE_STAGE" checkout --detach "$TOOL_REVISION"
  [[ "$(git -C "$SOURCE_STAGE" rev-parse HEAD)" == "$TOOL_REVISION" ]] \
    || fail "xtool source checkout does not match pinned revision $TOOL_REVISION"
  [[ -f "$SOURCE_STAGE/Package.resolved" ]] || fail "pinned xtool source is missing Package.resolved"
  [[ -z "$(git -C "$SOURCE_STAGE" status --porcelain)" ]] \
    || fail "pinned xtool source checkout is not clean: $SOURCE_STAGE"
  mv -- "$SOURCE_STAGE" "$SOURCE_DIR"
fi

[[ -d "$SOURCE_DIR/.git" ]] || fail "pinned xtool source checkout is missing: $SOURCE_DIR"
[[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$TOOL_REVISION" ]] \
  || fail "xtool source checkout has the wrong revision: $SOURCE_DIR"
[[ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ]] \
  || fail "pinned xtool source checkout contains local changes: $SOURCE_DIR"

mkdir -p "$PRIVATE_TOOL_DIR"
if [[ ! -x "$PRIVATE_TOOL" ]] || [[ "$("$PRIVATE_TOOL" --version 2>/dev/null || true)" != "xtool commit $TOOL_REVISION" ]]; then
  (
    cd "$SOURCE_DIR"
    contained swift build --only-use-versions-from-resolved-file --jobs "$BUILD_JOBS" --configuration "$BUILD_CONFIGURATION" --product "$BUILD_PRODUCT"
  )
  BUILT_TOOL="$SOURCE_DIR/.build/$BUILD_CONFIGURATION/$BUILD_PRODUCT"
  [[ -x "$BUILT_TOOL" ]] || fail "xtool build did not produce $BUILT_TOOL"
  VERSION_OUTPUT="$("$BUILT_TOOL" --version)"
  [[ "$VERSION_OUTPUT" == "xtool commit $TOOL_REVISION" ]] \
    || fail "built xtool reports an unexpected version: $VERSION_OUTPUT"
  PRIVATE_TOOL_TMP="$PRIVATE_TOOL_DIR/.xtool-install.$$"
  install -m 0755 "$BUILT_TOOL" "$PRIVATE_TOOL_TMP"
  mv -f -- "$PRIVATE_TOOL_TMP" "$PRIVATE_TOOL"
fi

SDK_SETTINGS_VALUE() {
  local settings="$1"
  swift -e 'import Foundation; let path = CommandLine.arguments[1]; let key = CommandLine.arguments[2]; let data = try Data(contentsOf: URL(fileURLWithPath: path)); let plist = try PropertyListSerialization.propertyList(from: data, options: [], format: nil) as! [String: Any]; print(plist[key] ?? "")' "$settings" "$2"
}

sdk_bundle_is_current() {
  local bundle="$1"
  local version_file="$bundle/darwin-sdk-version.txt"
  local xcode_info="$bundle/Xcode.app/Contents/Info.plist"
  local sdk_settings="$bundle/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS$SDK_VERSION.sdk/SDKSettings.plist"
  local bundle_version xcode_version sdk_name sdk_version

  [[ -d "$bundle" && ! -L "$bundle" && -d "$bundle/Xcode.app" ]] || return 1
  [[ -r "$version_file" && -r "$xcode_info" && -r "$sdk_settings" ]] || return 1
  bundle_version="$(cat "$version_file")"
  [[ "$bundle_version" == "$SDK_BUNDLE_VERSION" ]] || return 1
  xcode_version="$(SDK_SETTINGS_VALUE "$xcode_info" CFBundleShortVersionString)" || return 1
  [[ "$xcode_version" == "$SDK_VERSION" ]] || return 1
  sdk_name="$(SDK_SETTINGS_VALUE "$sdk_settings" CanonicalName)" || return 1
  sdk_version="$(SDK_SETTINGS_VALUE "$sdk_settings" Version)" || return 1
  [[ "$sdk_name" == "$SDK_CANONICAL_NAME" && "$sdk_version" == "$SDK_VERSION" ]]
}

for config_path in "$XTOOL_HOME" "$SDK_CANDIDATE_HOME" "$SDK_TARGET_CONFIG"; do
  [[ ! -L "$config_path" ]] || fail "refusing to write through symlinked xtool config path: $config_path"
done

if sdk_bundle_is_current "$SDK_TARGET_BUNDLE"; then
  echo "Using full Xcode $SDK_VERSION SDK in $SDK_TARGET_BUNDLE"
else
  [[ ! -L "$SDK_CANDIDATE_CONFIG" ]] || fail "candidate SDK config is a symlink: $SDK_CANDIDATE_CONFIG"
  if ! sdk_bundle_is_current "$SDK_CANDIDATE_BUNDLE"; then
    [[ ! -e "$SDK_CANDIDATE_BUNDLE" ]] \
      || fail "candidate SDK bundle has an unexpected version: $SDK_CANDIDATE_BUNDLE"
    [[ -f "$SDK_SOURCE" ]] || fail "Xcode $SDK_VERSION archive is missing: $SDK_SOURCE"
    printf '%s  %s\n' "$SDK_SHA256" "$SDK_SOURCE" | sha256sum --check --status \
      || fail "Xcode SDK archive checksum does not match the manifest: $SDK_SOURCE"
    mkdir -p "$SDK_CANDIDATE_CONFIG"
    contained env XDG_CONFIG_HOME="$SDK_CANDIDATE_CONFIG" "$PRIVATE_TOOL" sdk install "$SDK_SOURCE"
    sdk_bundle_is_current "$SDK_CANDIDATE_BUNDLE" \
      || fail "xtool installed an SDK that does not match Xcode $SDK_VERSION or bundle $SDK_BUNDLE_VERSION"
  fi

  SDK_STATUS="$(XDG_CONFIG_HOME="$SDK_CANDIDATE_CONFIG" "$PRIVATE_TOOL" sdk status)"
  grep -Fq "Path: $SDK_CANDIDATE_BUNDLE" <<<"$SDK_STATUS" || fail "xtool does not report the staged SDK path"
  grep -Fq "Flavor: normal" <<<"$SDK_STATUS" || fail "xtool SDK is not a full SDK"
  grep -Fq "Version: $SDK_BUNDLE_VERSION" <<<"$SDK_STATUS" || fail "xtool reports an unexpected SDK bundle version"

  if [[ -e "$SDK_TARGET_CONFIG" ]]; then
    [[ ! -L "$SDK_TARGET_CONFIG" ]] || fail "SDK target config is a symlink: $SDK_TARGET_CONFIG"
    OLD_VERSION="unknown"
    [[ ! -r "$SDK_TARGET_BUNDLE/darwin-sdk-version.txt" ]] \
      || OLD_VERSION="$(cat "$SDK_TARGET_BUNDLE/darwin-sdk-version.txt")"
    SAFE_OLD_VERSION="${OLD_VERSION//[^A-Za-z0-9_.-]/_}"
    SDK_BACKUP="$XTOOL_HOME/config-previous-$SAFE_OLD_VERSION"
    [[ ! -e "$SDK_BACKUP" ]] || fail "SDK backup already exists: $SDK_BACKUP"
    mv -- "$SDK_TARGET_CONFIG" "$SDK_BACKUP"
    if ! mv -- "$SDK_CANDIDATE_CONFIG" "$SDK_TARGET_CONFIG"; then
      mv -- "$SDK_BACKUP" "$SDK_TARGET_CONFIG"
      fail "could not promote candidate SDK config"
    fi
    echo "Preserved previous SDK config in $SDK_BACKUP"
  else
    mv -- "$SDK_CANDIDATE_CONFIG" "$SDK_TARGET_CONFIG"
  fi
fi

sdk_bundle_is_current "$SDK_TARGET_BUNDLE" || fail "installed SDK failed its final version and Settings.plist checks"
SDK_STATUS="$(XDG_CONFIG_HOME="$SDK_TARGET_CONFIG" "$PRIVATE_TOOL" sdk status)"
grep -Fq "Path: $SDK_TARGET_BUNDLE" <<<"$SDK_STATUS" || fail "xtool does not report the installed private SDK path"
grep -Fq "Flavor: normal" <<<"$SDK_STATUS" || fail "xtool SDK is not a full SDK"
grep -Fq "Version: $SDK_BUNDLE_VERSION" <<<"$SDK_STATUS" || fail "xtool reports an unexpected SDK bundle version"

TOOL_BIN="$LIMULATOR_PREFIX/bin/xtool"
printf -v DEFAULT_DATA_ROOT_QUOTED '%q' "$LIMULATOR_HOME"
TOOL_TMP="$LIMULATOR_PREFIX/bin/.xtool-wrapper.$$"
[[ ! -e "$TOOL_TMP" && ! -L "$TOOL_TMP" ]] || fail "wrapper staging path already exists: $TOOL_TMP"
cat > "$TOOL_TMP" <<EOF
#!/usr/bin/env bash
# Managed by Limulator bootstrap_xtool.sh
set -euo pipefail

SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
PREFIX="\$(cd "\$SCRIPT_DIR/.." && pwd)"
DEFAULT_DATA_ROOT=$DEFAULT_DATA_ROOT_QUOTED
DATA_ROOT="\${LIMULATOR_HOME:-\$DEFAULT_DATA_ROOT}"
SWIFT_VERSION="$SWIFT_VERSION"
TOOL_REVISION="$TOOL_REVISION"
REAL_XTOOL="\$PREFIX/libexec/limulator/xtool/\$TOOL_REVISION/xtool"
SWIFT_BIN="\$DATA_ROOT/swiftly/toolchains/\$SWIFT_VERSION/usr/bin"
SWIFT_LIB="\$DATA_ROOT/swiftly/toolchains/\$SWIFT_VERSION/usr/lib/swift/linux"

[[ -x "\$REAL_XTOOL" ]] || { echo "error: pinned xtool binary is missing: \$REAL_XTOOL" >&2; exit 1; }
[[ -x "\$SWIFT_BIN/swift" && -d "\$SWIFT_LIB" ]] || { echo "error: private Swift \$SWIFT_VERSION is missing; run tools/bootstrap_backend.sh swift first" >&2; exit 1; }
export PATH="\$SWIFT_BIN:\$PATH"
export LD_LIBRARY_PATH="\$PREFIX/lib:\$PREFIX/lib64:\$SWIFT_LIB\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export XDG_CONFIG_HOME="\${LIMULATOR_XTOOL_CONFIG:-\$DATA_ROOT/xtool/config}"
exec "\$REAL_XTOOL" "\$@"
EOF
chmod 0755 "$TOOL_TMP"

CLEAN_VERSION="$(env -i HOME="$HOME" PATH=/usr/bin:/bin "$TOOL_TMP" --version)"
[[ "$CLEAN_VERSION" == "xtool commit $TOOL_REVISION" ]] || fail "candidate xtool wrapper reports an unexpected version: $CLEAN_VERSION"
CLEAN_STATUS="$(env -i HOME="$HOME" PATH=/usr/bin:/bin "$TOOL_TMP" sdk status)"
grep -Fq "Path: $SDK_TARGET_BUNDLE" <<<"$CLEAN_STATUS" || fail "candidate wrapper does not use the private SDK path"
grep -Fq "Flavor: normal" <<<"$CLEAN_STATUS" || fail "candidate wrapper does not use a full SDK"

if [[ -e "$TOOL_BIN" || -L "$TOOL_BIN" ]]; then
  if ! grep -Fq '# Managed by Limulator bootstrap_xtool.sh' "$TOOL_BIN" 2>/dev/null; then
    [[ -f "$TOOL_BIN" || -L "$TOOL_BIN" ]] || fail "existing xtool path is not a file: $TOOL_BIN"
    TOOL_BACKUP="$TOOL_BIN.legacy-$TOOL_VERSION"
    [[ ! -e "$TOOL_BACKUP" && ! -L "$TOOL_BACKUP" ]] || fail "existing xtool backup already exists: $TOOL_BACKUP"
    TOOL_BACKUP_TMP="$TOOL_BACKUP.tmp.$$"
    [[ ! -e "$TOOL_BACKUP_TMP" && ! -L "$TOOL_BACKUP_TMP" ]] || fail "xtool backup staging path already exists: $TOOL_BACKUP_TMP"
    cp -Pp -- "$TOOL_BIN" "$TOOL_BACKUP_TMP"
    mv -- "$TOOL_BACKUP_TMP" "$TOOL_BACKUP"
    TOOL_BACKUP_TMP=""
    echo "Preserved previous xtool executable in $TOOL_BACKUP"
  fi
fi

mv -f -- "$TOOL_TMP" "$TOOL_BIN"
TOOL_TMP=""

echo "$CLEAN_VERSION"
echo "$CLEAN_STATUS"
