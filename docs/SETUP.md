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

Resource-intensive operations use a systemd scope with a 32 GiB soft memory limit and 40 GiB hard limit. Backend builds, creation, and headless guests allow up to 4 GiB of swap; native interactive windows disable swap to keep their working memory resident. They require at least 32 GiB available host RAM and 4 GiB free swap before starting. Available RAM is the current headroom, not the machine's installed memory; a 64 GiB or larger workstation is recommended. Allow roughly 100 GiB free disk for one profile, or 200 GiB for all three profiles, the SDK, firmware caches, and restore work. Preserved recovery disks require additional space.

The native vphone CLI uses a Release build. After updating its pinned source revision, rebuild it with `uv run limulator bootstrap cli`; an absent Release product stops creation instead of triggering an implicit build. The xtool host executable uses a Debug build to avoid a Swift 6.4.1 optimizer crash. The app's `--configuration` setting remains independent. Older installations with locally applied dependency patches need the [source migration steps](UPSTREAM.md#updating-an-existing-installation).

The default data directory is `~/.local/share/limulator`. `LIMULATOR_HOME` or the CLI's `--data` selects another directory. Source and tools live under `backends/` and `tools/`; VM bundles live under `vms/`; device registrations live under `devices/`; runtime logs and sockets live under `runs/`. Firmware stays in the local cache. These files do not belong in Git.

## Create and start a device

List the pinned profiles before creating a device:

```sh
uv run limulator profiles
uv run limulator --device duo create --profile iphone-duo --cid 41
uv run limulator --device iphone-16 create --profile iphone-16 --cid 42
uv run limulator --device ipad-mini create --profile ipad-mini-a17-pro --cid 43
```

The profiles are iPhone Duo on iOS 27.1, the base iPhone 16 on iOS 27.0.1, and iPad mini A17 Pro on iOS 27.0.1. Each profile selects its product type, OS, native geometry, and scale. These guest settings do not identify the host Vulkan device or establish that device's GPU capabilities. An explicit model, OS, or scale that contradicts `--profile` is rejected before creation. Use a distinct device name for each guest. You can omit `--cid`: creation reuses that device's saved specification or selects a free CID starting at 41. An explicit CID that belongs to another registered or pending device is rejected before restore. Running `create` without profile options still selects Duo. The catalog distinguishes upstream Linux testing from profiles whose runtime verification is pending; an available profile does not establish that its guest has run on this workstation.

### Choose a graphics policy

New `create`, `start`, and `run` launches use compatible mode by default. Each command accepts `--graphics-mode compatible`, `--graphics-mode strict`, or `--graphics-mode diagnostic`. `LIMULATOR_GRAPHICS_MODE` selects a mode when the command-line option is omitted; the command-line option takes precedence.

Compatible mode follows pinned Reims vGPU's normal policy: strict mode is off and census is disabled. It relays ordinary precision notices while retaining structural failures classified by Reims as `DIES_BY_DEFAULT`. Strict mode turns on Reims's stricter failure policy and may reject additional device-specific numeric differences. Diagnostic mode enables a census in the private run directory; Reims can continue through a broader set of differences, so use it for exploration rather than acceptance. Keep stderr warnings with the run evidence. No mode promises full Metal or physical GPU equivalence. See the [pinned Reims vGPU API](https://github.com/YahiaAngelo/reims-vgpu/blob/960bf4b128a2678f032e46eb32964caa4828b818/docs/api.md#L229-L257) and its [pinned answer fate rules](https://github.com/YahiaAngelo/reims-vgpu/blob/960bf4b128a2678f032e46eb32964caa4828b818/crates/reims-vgpu-vulkan/src/answer.rs#L1683-L1695).

Graphics mode applies to a new creation or runtime launch. Limulator does not change a running guest to satisfy a requested mode. A `run` without a selected mode reuses the running guest; an explicit mismatch, including a legacy session whose mode is unknown, fails and asks you to stop and start with the requested mode. `status` reports `graphics_policy.mode: unknown` for older runtime records and never infers a value from the current environment. Stop the guest, then use `start --graphics-mode compatible`, `start --graphics-mode strict`, or `start --graphics-mode diagnostic` to record the next launch's mode. An incomplete, unregistered creation can select a mode when it resumes; without an explicit selection, Limulator preserves its recorded mode when one is available.

```sh
uv run limulator --device duo create --model iPhone19,4 --ios 27.1 --cid 41 --scale 3
uv run limulator devices
uv run limulator --device duo status
```

Creation verifies firmware hashes, restores a stock cloudOS image to obtain the GPU driver, prepares and patches the iOS firmware, restores the main guest, installs its research components, and waits for an unlocked screen. First boot includes data migration and can take much longer than later starts. Creation logs identify the current stage and can be inspected while the existing process runs. Do not start a second creation just because the current screen or log has not changed recently.

Before changing Setup Assistant or unlocking the screen, creation requires DataMigrator to report boolean `success: true` for the expected iPhone firmware build. The backend reads that identity from the prepared tree's original `iPhone-BuildManifest.plist`, which remains usable if the cached IPSW was renamed or removed. A matching guest build marker alone is insufficient. Missing or stale results wait within the migration timeout. An explicit failure for the expected build stops creation and preserves the disk and logs for inspection. Do not force setup completion or edit migration markers to bypass the failure. If creation reaches final host verification, that check independently validates the result and records it in the creation receipt. An early backend failure records an error and logs but may not populate the receipt's structured `migration_result` field.

New creation runs expose a private QMP socket during first boot. The local `.limulator-create.json` receipt records its path as `first_boot_qmp`; it belongs to that run and ends with the guest process. This permits GPU framebuffer capture while guest screenshot services are still starting. Existing running guests keep their original launch configuration.

The creator preserves incomplete images and resumes completed stages. It uses a private USB daemon, scoped virtual-device access, and exact process identities. It refuses a conflicting physical Apple device or an existing owner of the guest disk. Never boot the same disk through another launcher while Limulator owns it.

For a one-off, local USB restore trace, prefix the create command with `VPHONE_USB_TRACE=1`. Limulator prints the private JSONL trace path under that run's directory. The trace records reset and descriptor-control outcomes plus USB/IP and host attach state changes. It is disabled by default and contains no USB payloads, descriptor bytes, pairing records, or device serial identifiers. It does not observe whether libimobiledevice sent or the guest accepted `StartRestore`.

Backend controls use the native `VPHONE_*` names. The older `LIMULATOR_*` USB and creation-timeout controls remain compatibility inputs to Limulator when their native counterparts are unset; they are translated locally rather than forwarded as upstream interfaces. Trace output always belongs to the current private run directory.

The QEMU fork provides a separate, default-off diagnostic allowlist: `vusb_host_enqueue`, `vusb_pci_rx_complete`, `vusb_virtio_notify`, and `vusb_pci_notify_route`. After checking out the locked sources, run `LIMULATOR_JOBS=4 uv run limulator bootstrap qemu` to rebuild QEMU; an existing binary cannot emit new events. Enable only these exact events in a private QEMU trace configuration when investigating control enumeration. The first two events cover complete EP0 or reset frames and successful receive copies submitted through `virtqueue_push`; the last two cover VUSB notification decisions and PCI routing, including any bulk-queue notifications. Fields are monotonic microsecond timestamps, endpoint/type codes, lengths, backlog, transport ownership, queue/vector values, and notification booleans. These events do not read transfer payloads or prove guest handling, interrupt delivery, or `StartRestore` acceptance. Keep `vusb_pci_rx` disabled because it includes payload-head bytes.

For a narrower MSI-X decision check, the fork also provides `vusb_msix_notify` and `vusb_msix_pending_replay`. These record vector usage, mask decisions and pending replay for the VUSB device without logging MSI addresses, MSI data or USB payloads. A send-returned event records the host code path, not guest interrupt handling.

If host verification fails after first boot, rerun the same `create` command. For a matching incomplete, unregistered guest, Limulator preserves the previous stage record and reruns boot and verification while keeping restore and guest installation complete. It refuses this recovery while another process owns the disk. Use `start` for a guest that is already registered.

Linux dyld-cache patching stages the selected cache family and undo log locally before publishing changed files. Publication updates one file at a time. An interrupted publication retains its before and after copies and a `.vphone-dsc-pending.json` journal in the VM bundle. While that journal exists, `start`, CFW retries, and `--fresh-disk` refuse to proceed. Preserve the disk, journal, and recovery directory named in the error; removing the journal does not repair a partial update. Recovery staging needs persistent storage. Filesystem sync cannot preserve a stage stored on tmpfs across a reboot.

Linux CFW updates also refuse a guest with `System/Library/xpc/launchd.plist.bak` but no valid Guest patch receipt, even without a pending journal. That backup can appear before cache publication finishes. This includes older installations without receipts. Preserve the disk and retained CFW artifacts for inspection; there is no automatic recovery override.

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

`start --window` opens the native emulator window with Home, Lock, volume, and Screenshot controls to the right of the device. Home wakes and swipes a locked phone; on an unlocked phone it sends Home. Power wakes a sleeping display or sends the virtual side-button action. Passcodes require your input. Screenshots save only when you press Screenshot. The toolbar uses the private control socket of that run and ends with its emulator. The browser console works with a headless guest. The `running` state confirms guest daemon health. It does not establish that the screen is awake or unlocked.

In the console, the destination picker lists registered devices and profiles that can be created. Selecting a registered device shows its OS, geometry, support status, and named Start and Stop controls. Stop waits for the owned guest to shut down and rejects duplicate requests while it is in progress. Duo alone exposes fold controls. Selecting a profile without a registered device shows a copyable creation command; creation and firmware downloads run through the terminal. Refresh the picker after registering or creating a device.

Each console request targets its device by name. Switching the picker clears the old display and ignores responses from the previous selection. The `serve --device` selection remains the default for clients that omit a device name.

## Build and run an app

For repeated app development on an existing guest, follow [Build, deploy, and test apps](APP_WORKFLOW.md). Keep that guest and its build caches; another app does not require another restore.

The xtool bootstrap installs a private xtool executable and its full Darwin SDK from your own Xcode archive. Then build and run CounterLab:

```sh
uv run limulator --device duo run examples/CounterLab
```

You can select another xtool executable explicitly when needed:

```sh
uv run limulator --device duo run examples/CounterLab --xtool /path/to/xtool
```

Limulator runs `xtool dev build --configuration debug --ipa`, inspects the resulting device binary, uploads it, and asks the guest to install and sign it. Host-side Apple distribution credentials are not required for this research guest. Simulator binaries and encrypted App Store executables are rejected. The pinned guest launch API does not accept app launch arguments; Limulator rejects them rather than ignoring them.

`run` starts the guest if needed, then wakes and unlocks it after installation and immediately before launch. A known no-passcode Home timeout triggers one bounded touch-unlock fallback; other unlock errors stop the command before launch. The separate `launch` command does not wake or unlock the guest.

You can also install an existing artifact:

```sh
uv run limulator install path/to/Example.ipa
uv run limulator unlock
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

Auto-Lock can lock the guest again while it is idle. Use **Unlock** in the console or `uv run limulator --device duo unlock` when the screen is off or locked. If the guest requires a passcode, unlocking reports an error; Limulator does not supply or bypass the passcode.

Tap and swipe coordinates refer to the current upright screenshot's pixels. Capture again after resizing or folding. The web console carries the frame geometry with pointer input and rejects stale coordinates. Accessibility identifiers provide a more stable interface for automated app tests. A screenshot command refuses to overwrite an existing file.

The browser service binds only to loopback, requires a session token for changes, and rejects cross-origin requests. Its empty state does not contain a stock iPhone image or a simulated app screenshot.

## Run app checks

```sh
uv run limulator test examples/CounterLab/scenarios/smoke.json --output artifacts/counterlab-smoke
uv run limulator test examples/CounterLab/scenarios/duo.json --output artifacts/counterlab-duo
```

Each run requires a new output directory. It writes `report.json` and genuine guest PNGs with hashes. A failed assertion produces a failing command and attempts a failure capture. If capture itself fails, the report records the error without inventing an image.

The report records the runtime's `graphics_policy`, including its mode and inherited `REIMS_VGPU_EXACT` setting when known. It does not infer historical settings from the current shell. A passing scenario establishes its tested UI behavior, not full Metal equivalence; retain the backend stderr warnings with acceptance evidence.

Semantic taps only select visible accessibility elements. To reach a control below the viewport, add an explicit scroll action before its tap:

```json
{"action": "scroll_to_element", "identifier": "counterlab.details.link", "direction": "down", "max_swipes": 6, "timeout": 30}
```

`direction` is `down` toward later content or `up` toward earlier content. It defaults to `down`; `max_swipes` defaults to 6 and accepts integers from 0 through 12; `timeout` defaults to 30 seconds and must be positive and at most 120 seconds. The action queries the guest's flat AX snapshot and `device.screen` point bounds. It accepts the exact identifier only when visible with an activation point inside those bounds. It captures a fresh framebuffer before each proportional swipe. It does not tap the target, retry transport failures, or infer UIKit point bounds from profile pixels. Reports include the swipe count, activation point, and daemon-reported point bounds.

`device.screen` uses the same IcliKit interface metrics as accessibility visibility and semantic tap validation. These metrics come from the UIKit screen capture and SpringBoard orientation, with a fallback to the main panel. They do not establish an experimental Duo inner window's UIKit geometry; that mapping still needs native verification.

The search deadline is checked between the existing bounded capture and swipe operations and before accepting a match. An in-flight capture or swipe can finish after that deadline, at which point the action fails. A missing target fails at the swipe limit or deadline and attempts the usual failure capture. Use a separate `tap_element` action after scrolling; use an upward scroll to return to the counter after visiting lower cards.

The smoke scenario checks a specific counter value, native text input, navigation, and count persistence after relaunch. The Duo scenario checks the counter across cover, book, and open states. A passing host-side unit suite alone does not establish that either native scenario works; native results must come from a running guest.
