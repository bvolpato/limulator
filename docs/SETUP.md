# Setup and use

Limulator runs a real iOS research guest on x86_64 Linux. It uses CPU translation and the host's Vulkan GPU. It does not require a remote Mac. The pinned creation profile is iPhone Duo with iOS 27.1 at its native 3x scale; the application can be compiled with SDK 27.2 as long as its deployment target supports the guest OS.

## Install the tools

The native bootstrap targets the Ubuntu 24.04 family, including Linux Mint 22.3. It builds newer Apple USB libraries and Nettle into a private prefix, rather than upgrading the operating system. Rust/Cargo, uv, an NVIDIA Vulkan driver, a user systemd manager, and sudo access are prerequisites. Restore and firmware patching need root access for virtual USB and mounts of the guest images.

Native creation helpers use `/usr/bin/python3` with Linux VSOCK support. Limulator itself supports both the system Python and uv-managed Python; each native run selects the helper interpreter through its own private wrapper.

```sh
uv sync --python 3.12
uv run limulator bootstrap deps
uv run limulator bootstrap all
uv run limulator bootstrap xtool
uv run limulator doctor
```

The backend and xtool bootstraps pin their source revisions and downloaded tools. Xtool is built from its pinned Swift source using the private Swift toolchain. It installs the full Darwin SDK from your Xcode 27.2 archive and reads the SDK version from its settings when it links the app. Set `LIMULATOR_XCODE_XIP` if the archive is not at `~/Downloads/Xcode_27.2.xip`.

Resource-intensive operations use a systemd scope with a 32 GiB soft memory limit, 40 GiB hard limit, and 4 GiB swap limit. They require at least 32 GiB available host RAM and 4 GiB free swap before starting. Available RAM is the current headroom, not the machine's installed memory; a 64 GiB or larger workstation is recommended. Allow roughly 100 GiB free disk for one profile, or 200 GiB for all three profiles, the SDK, firmware caches, and restore work. Preserved recovery disks require additional space.

The native vphone CLI uses a Release build. After updating its patches, rebuild it with `uv run limulator bootstrap cli`; an absent Release product stops creation instead of triggering an implicit build. The xtool host executable uses a Debug build to avoid a Swift 6.4.1 optimizer crash. The app's `--configuration` setting remains independent.

The default data directory is `~/.local/share/limulator`. `LIMULATOR_HOME` or the CLI's `--data` selects another directory. Source and tools live under `backends/` and `tools/`; VM bundles live under `vms/`; device registrations live under `devices/`; runtime logs and sockets live under `runs/`. Firmware stays in the local cache. These files do not belong in Git.

## Create and start a device

List the pinned profiles before creating a device:

```sh
uv run limulator profiles
uv run limulator --device duo create --profile iphone-duo --cid 41
uv run limulator --device iphone-16 create --profile iphone-16 --cid 42
uv run limulator --device ipad-mini create --profile ipad-mini-a17-pro --cid 43
```

The profiles are iPhone Duo on iOS 27.1, the base iPhone 16 on iOS 27.0.1, and iPad mini A17 Pro on iOS 27.0.1. Each profile selects its product type, OS, native geometry, and scale. An explicit model, OS, or scale that contradicts `--profile` is rejected before creation. Use a distinct device name for each guest. You can omit `--cid`: creation reuses that device's saved specification or selects a free CID starting at 41. An explicit CID that belongs to another registered or pending device is rejected before restore. Running `create` without profile options still selects Duo. The catalog distinguishes upstream Linux testing from profiles whose runtime verification is pending; an available profile does not establish that its guest has run on this workstation.

```sh
uv run limulator --device duo create --model iPhone19,4 --ios 27.1 --cid 41 --scale 3
uv run limulator devices
uv run limulator --device duo status
```

Creation verifies firmware hashes, restores a stock cloudOS image to obtain the GPU driver, prepares and patches the iOS firmware, restores the main guest, installs its research components, and waits for an unlocked screen. First boot includes data migration and can take much longer than later starts. Creation logs identify the current stage and can be inspected while the existing process runs. Do not start a second creation just because the current screen or log has not changed recently.

Final creation verification requires DataMigrator to report `success: true` for the pinned firmware build. A matching build marker alone is insufficient. The creation receipt records the actual migration result, including a reported failure.

New creation runs expose a private QMP socket during first boot. The local `.limulator-create.json` receipt records its path as `first_boot_qmp`; it belongs to that run and ends with the guest process. This permits GPU framebuffer capture while guest screenshot services are still starting. Existing running guests keep their original launch configuration.

The creator preserves incomplete images and resumes completed stages. It uses a private USB daemon, scoped virtual-device access, and exact process identities. It refuses a conflicting physical Apple device or an existing owner of the guest disk. Never boot the same disk through another launcher while Limulator owns it.

If host verification fails after first boot, rerun the same `create` command. For a matching incomplete, unregistered guest, Limulator preserves the previous stage record and reruns boot and verification while keeping restore and guest installation complete. It refuses this recovery while another process owns the disk. Use `start` for a guest that is already registered.

Linux dyld-cache patching stages the selected cache family and undo log locally before publishing changed files. Publication updates one file at a time. An interrupted publication retains its before and after copies and a `.vphone-dsc-pending.json` journal in the VM bundle. While that journal exists, `start`, CFW retries, and `--fresh-disk` refuse to proceed. Preserve the disk, journal, and recovery directory named in the error; removing the journal does not repair a partial update. Recovery staging needs persistent storage. Filesystem sync cannot preserve a stage stored on tmpfs across a reboot.

An interrupted dyld-cache patch needs an integrity check before repair. With the guest stopped and its System volume mounted read-only, run `uv run tools/audit_dyld_cache.py /path/to/dyld_shared_cache_arm64e_x1`. The tool checks SHA-256 code-page slots in the main cache and its declared subcaches, reports coverage and mismatches as JSON, and fails on unsupported or incomplete input. It records the unsigned `.atlas` metadata separately. It does not validate CMS trust, certify original instructions, or change cache bytes. A successful page audit alone does not resolve a pending publication or establish app compatibility.

If an interrupted restore leaves partial APFS media and a later attempt reports `newfs_apfs: /dev/disk1 is already a synthesized APFS device: Resource busy`, the explicit recovery option is:

```sh
uv run limulator --device duo create --profile iphone-duo --fresh-disk
```

This option is limited to a matching incomplete creation that has no registered device or active disk owner. It preserves the previous disk under a unique name before preparing a fresh sparse disk. Use the same device name and creation specification as the failed attempt. Plain `create` never replaces the disk; `--fresh-disk` is an opt-in recovery step, not a way to reset a registered guest.

After an ordinary shutdown:

```sh
uv run limulator --device duo start --wait 600
uv run limulator --device duo stop
```

`start --window` also opens the native emulator window. The browser console works with a headless guest.

In the console, the destination picker lists registered devices and profiles that can be created. Selecting a registered device shows its OS, geometry, support status, and named Start and Stop controls. Stop waits for the owned guest to shut down and rejects duplicate requests while it is in progress. Duo alone exposes fold controls. Selecting a profile without a registered device shows a copyable creation command; creation and firmware downloads run through the terminal. Refresh the picker after registering or creating a device.

Each console request targets its device by name. Switching the picker clears the old display and ignores responses from the previous selection. The `serve --device` selection remains the default for clients that omit a device name.

## Build and run an app

The xtool bootstrap installs a private xtool executable and its full Darwin SDK from your own Xcode archive. Then build and run CounterLab:

```sh
uv run limulator --device duo run examples/CounterLab
```

You can select another xtool executable explicitly when needed:

```sh
uv run limulator --device duo run examples/CounterLab --xtool /path/to/xtool
```

Limulator runs `xtool dev build --configuration debug --ipa`, inspects the resulting device binary, uploads it, and asks the guest to install and sign it. Host-side Apple distribution credentials are not required for this research guest. Simulator binaries and encrypted App Store executables are rejected. The pinned guest launch API does not accept app launch arguments; Limulator rejects them rather than ignoring them.

You can also install an existing artifact:

```sh
uv run limulator install path/to/Example.ipa
uv run limulator launch com.example.Example
```

## Interact and capture

```sh
uv run limulator serve
# Open http://127.0.0.1:18765
uv run limulator screenshot artifacts/example.png
uv run limulator tap 300 500
uv run limulator tap-element counterlab.increment
uv run limulator text 'Hello from Linux'
uv run limulator fold open
uv run limulator tree
```

Tap and swipe coordinates refer to the current upright screenshot's pixels. Capture again after resizing or folding. The web console carries the frame geometry with pointer input and rejects stale coordinates. Accessibility identifiers provide a more stable interface for automated app tests. A screenshot command refuses to overwrite an existing file.

The browser service binds only to loopback, requires a session token for changes, and rejects cross-origin requests. Its empty state does not contain a stock iPhone image or a simulated app screenshot.

## Run app checks

```sh
uv run limulator test examples/CounterLab/scenarios/smoke.json --output artifacts/counterlab-smoke
uv run limulator test examples/CounterLab/scenarios/duo.json --output artifacts/counterlab-duo
```

Each run requires a new output directory. It writes `report.json` and genuine guest PNGs with hashes. A failed assertion produces a failing command and attempts a failure capture. If capture itself fails, the report records the error without inventing an image.

Semantic taps only select visible accessibility elements. To reach a control below the viewport, add an explicit scroll action before its tap:

```json
{"action": "scroll_to_element", "identifier": "counterlab.details.link", "direction": "down", "max_swipes": 6, "timeout": 30}
```

`direction` is `down` toward later content or `up` toward earlier content. It defaults to `down`; `max_swipes` defaults to 6 and accepts integers from 0 through 12; `timeout` defaults to 30 seconds and must be positive and at most 120 seconds. The action queries the guest's flat AX snapshot and `device.screen` point bounds. It accepts the exact identifier only when visible with an activation point inside those bounds. It captures a fresh framebuffer before each proportional swipe. It does not tap the target, retry transport failures, or infer UIKit point bounds from profile pixels. Reports include the swipe count, activation point, and daemon-reported point bounds.

`device.screen` uses the same IcliKit interface metrics as accessibility visibility and semantic tap validation. These metrics come from the UIKit screen capture and SpringBoard orientation, with a fallback to the main panel. They do not establish an experimental Duo inner window's UIKit geometry; that mapping still needs native verification.

The search deadline is checked between the existing bounded capture and swipe operations and before accepting a match. An in-flight capture or swipe can finish after that deadline, at which point the action fails. A missing target fails at the swipe limit or deadline and attempts the usual failure capture. Use a separate `tap_element` action after scrolling; use an upward scroll to return to the counter after visiting lower cards.

The smoke scenario checks a specific counter value, native text input, navigation, and count persistence after relaunch. The Duo scenario checks the counter across cover, book, and open states. A passing host-side unit suite alone does not establish that either native scenario works; native results must come from a running guest.
