# Dependency forks and upstream contributions

Limulator pins complete dependency commits instead of applying its own patch files during bootstrap. Each fork's `dev` branch contains the changes needed together by Limulator. The lock uses full commit identifiers, so moving a branch does not change an existing installation.

| Component | Integration fork | Upstream project |
|---|---|---|
| Linux launcher, restore tooling, Linux source overlays and native toolbar | [bvolpato/vphone-linux](https://github.com/bvolpato/vphone-linux/tree/dev) | [YahiaAngelo/vphone-linux](https://github.com/YahiaAngelo/vphone-linux) |
| Native CLI, dyld reader, bundled recovery client and device definitions | [bvolpato/vphone-cli](https://github.com/bvolpato/vphone-cli/tree/dev) | [Lakr233/vphone-cli](https://github.com/Lakr233/vphone-cli) |
| ARM emulator and optional USB transport diagnostics | [bvolpato/qemu-vphone](https://github.com/bvolpato/qemu-vphone/tree/dev) | [YahiaAngelo/qemu-vphone](https://github.com/YahiaAngelo/qemu-vphone) |

Xtool already uses its own pinned fork revision and the existing [platform-build PR #362](https://github.com/xtool-org/xtool/pull/362). Its branch is separate from this migration. Limulator's xtool installation wrapper and SDK path selection remain Limulator code.

Limulator owns app builds and deployment, device registration, process and disk ownership, workstation resource limits, the local console, and acceptance scenarios. These stay here. Linux-specific Swift overlays belong to the Linux backend; portable corrections belong in the CLI source itself. A `dev` integration branch is not an upstream PR: contributions use separate branches and focused changes.

The Linux backend still carries its own APFS compatibility patch and Swift source overlays. Those are existing parts of that dependency's Linux port. This migration removes Limulator's additional patch series, not every transformation performed inside upstream's build.

## Component contributions

Numbers below identify the [former patch series](https://github.com/bvolpato/limulator/tree/b5b93902c649a0f7364505705282d0f9d3e48d80/patches). They are historical references, not files required by bootstrap. The later `0021` diagnostic is included directly in the QEMU fork.

| Former patches | Component | Upstream proposal |
|---|---|---|
| `0001`, `0002`, `0003`, `0007` | Setup preservation, bounded waits, owned cleanup and loopback GDB | [vphone-linux #26](https://github.com/YahiaAngelo/vphone-linux/pull/26) |
| `0004`, `0010` | USB/IP unlink framing and host-owned bulk waits | [vphone-linux #25](https://github.com/YahiaAngelo/vphone-linux/pull/25) |
| `0005` | Bundled recovery-client lifetime | [vphone-cli #854](https://github.com/Lakr233/vphone-cli/pull/854) |
| `0009` | iPhone 16 display geometry | [vphone-cli #855](https://github.com/Lakr233/vphone-cli/pull/855) |
| `0011` | Dyld mapping lifetime and Linux worker bound | [vphone-cli #853](https://github.com/Lakr233/vphone-cli/pull/853) |
| `0012` | Optional private QMP socket during first boot | [vphone-linux #27](https://github.com/YahiaAngelo/vphone-linux/pull/27) |
| `0013`, `0015`, `0017` | Linux cache staging, durable publication, recovery journal and receipts | [vphone-linux #31](https://github.com/YahiaAngelo/vphone-linux/pull/31) |
| `0016` | Release CLI selection and explicit builds | [vphone-linux #30](https://github.com/YahiaAngelo/vphone-linux/pull/30) |
| `0019` | Optional USB transport metadata trace | [vphone-linux #28](https://github.com/YahiaAngelo/vphone-linux/pull/28), stacked on #25 |
| `0020`, `0021` | Optional USB, VirtIO and MSI-X diagnostics | [qemu-vphone #2](https://github.com/YahiaAngelo/qemu-vphone/pull/2) |
| `0022`, `0023` | Per-run native window socket and guest controls | [vphone-linux #29](https://github.com/YahiaAngelo/vphone-linux/pull/29) |

As of 8 November 2026 UTC, CLI [#853](https://github.com/Lakr233/vphone-cli/pull/853), [#854](https://github.com/Lakr233/vphone-cli/pull/854), [#855](https://github.com/Lakr233/vphone-cli/pull/855), and [#856](https://github.com/Lakr233/vphone-cli/pull/856) have merged upstream. The remaining contributions above are open proposals. The lock still selects the tested integration revisions; an upstream merge alone does not validate a newer CLI or its guest resources.

The QEMU PR uses generic VirtIO and PCI trace names; the pinned `dev` source retains the VUSB-specific names of its validated build. Do not use one version's event allowlist with the other without checking `-trace help`.

A later native CFW installation exposed a separate subprocess capture hang: the child had exited, but Linux Foundation omitted an EOF readability callback. The merged [vphone-cli #856](https://github.com/Lakr233/vphone-cli/pull/856) replaces callback-based collection with concurrent pipe readers. The standalone regression reproduces the old hang and checks complete output and exit status with the replacement. This fix is separate from the former patch migration.

[vphone-linux #40](https://github.com/YahiaAngelo/vphone-linux/pull/40) corrects the catalog names for `iPhone17,3` and `iPad16,1` to “iPhone 16” and “iPad mini (A17 Pro),” and fixes the matching iPhone README examples. Catalog validation, the device list, and setup dry runs verify the displayed names.

[vphone-linux #41](https://github.com/YahiaAngelo/vphone-linux/pull/41) fixes toolbar startup on Linux Python builds that omit `socket.AF_VSOCK`. The fallback packs the Linux socket address and uses a bounded nonblocking connection; Python builds with native VSOCK support retain their existing path. Regression tests, a guest-free kernel loopback, and a read-only guest screen query validate the connection. These checks do not establish toolbar button behavior.

The native-controls contribution in [vphone-linux #29](https://github.com/YahiaAngelo/vphone-linux/pull/29) also fixes Duo discovery before the guest initializes its hinge state. An initial orientation reply can omit `fold_angle`; treating that omission as a permanent non-foldable result hid Fold and stopped later orientation updates. The launcher now passes the guest configuration, so a known Duo exposes Fold immediately and a known ordinary phone avoids hinge polling. Missing or malformed metadata retains discovery from a later positive hinge reply. Regression tests reproduce the old failure, and combined toolbar/VSOCK tests pass on the pinned integration commit. Native validation of this toolbar revision remains pending.

[vphone-linux #42](https://github.com/YahiaAngelo/vphone-linux/pull/42) makes an explicit `VPHONE_ECID` a strict device selector. A missing, ambiguous, or conflicting match fails without trying another attached device. [vphone-linux #43](https://github.com/YahiaAngelo/vphone-linux/pull/43) binds Linux CFW mounts to the selected bundle's ECID, passes paths literally, and keeps askpass helpers private to the run. These are separate contributions: the caller supplies the identity, and the connector enforces it.

[vphone-linux #44](https://github.com/YahiaAngelo/vphone-linux/pull/44) fixes isolated Swiftly installation. Setting only `SWIFTLY_HOME_DIR` let Swiftly install its binary in the user's default directory, then setup failed when invoking the missing private executable. The fix respects an explicit `SWIFTLY_BIN_DIR` and otherwise keeps the executable under the configured home. Local validation and fork CI exercise private initialization with the signed Swiftly 1.2.0 binary without installing a toolchain or changing shell profiles.

[vphone-linux #45](https://github.com/YahiaAngelo/vphone-linux/pull/45) lets setup use `VSETUP_CACHE` for firmware downloads, cached GPU-driver lookup and free-space checks. This allows large firmware files to live on another filesystem while preserving the default `~/.cache/vphone`. Limulator uses the same resolved directory for hash verification and native creation inputs. Shell regression checks and fork CI pass; a clean iPad creation with the private cache remains pending.

[Icli #19](https://github.com/owngoal-dev/icli/pull/19) adds an optional geometry scope for library callers that obtain active-display coordinates from the OS. One validated snapshot controls point validation, AX visibility and HID normalization, with independent concurrent scopes and restoration after nested or throwing calls. This library API is separate from the daemon's FrontBoard display discovery. Screenshot, OCR and screen-description calls reject the override because geometry alone does not bind a capture source.

The geometry API's 0.10 backport compiled into an arm64 iOS worker, and a native Duo inner-display accessibility tap changed CounterLab's counter from 2 to 3. The upstream 0.11 package still needs a complete Apple build. A later experimental worker captured BruTV's native inner-display pixels, but general app input, capture orientation and broader fold scenarios remain separate acceptance gates. The experimental worker and its Icli fork are not selected by the default lock.

[vphone-cli #861](https://github.com/Lakr233/vphone-cli/pull/861) passes an explicit `SWIFTPM_MIRROR_CONFIG` through the nested signer's isolated build environment. Previously, that build discarded the caller's mirror configuration and retried failing artifact URLs. Shell checks exercise the actual build phase with unset, empty and literal paths. Artifact URL mappings live in the fork's build configuration.

[vphone-cli #862](https://github.com/Lakr233/vphone-cli/pull/862) moves health dependencies off the HTTP event loop. Concurrent requests share one producer and a bounded deadline; an overdue producer keeps its admission slot until it returns. The standalone Swift harness and an arm64 iOS worker build validate the shared implementation. This does not establish the cause of the research guest's cold-boot delay or prove runtime recovery. Both fixes are proposed separately from the display experiments.

[vphone-cli #864](https://github.com/Lakr233/vphone-cli/pull/864) adds read-only active-display geometry discovery without changing input or capture. A bounded FrontBoard observation reports native display identity, scale, bounds and orientation. A timed-out request retains admission until its queued main-thread work drains, preventing repeated timeouts from accumulating work. An Apple-runner regression reproduces the previous admission failure and passes with the fix; the [upstream build passes](https://github.com/Lakr233/vphone-cli/actions/runs/42473541656). This proposal uses the upstream Icli dependency and can be reviewed independently of the geometry consumer and raw-capture experiments.

[vphone-cli #865](https://github.com/Lakr233/vphone-cli/pull/865) compares verified executable file identity when selecting and confirming a cached worker. Equivalent executable path spellings previously failed literal string comparisons. Root ownership, no-follow cache access, digest, executable-bit and post-hash identity checks remain enforced. The proposal also logs confirmation outcomes. Native C regression checks and the existing Swift logic suite pass on an [Apple runner](https://github.com/bvolpato/vphone-cli/actions/runs/42476723294) with identical source files. The historical cold-boot fallback cause remains unconfirmed, and a cold-boot persistence check remains pending.

The lock pins [integration commit `04a8f6c`](https://github.com/bvolpato/vphone-linux/commit/04a8f6c1a3e290a2e352ab814158054de58442c6), which combines the firmware-cache and Swiftly fixes with integer migration-result handling and CFW target binding. [Integration CI passes](https://github.com/bvolpato/vphone-linux/actions/runs/42467007570). A fresh isolated Linux Release CLI build at parent `e98bfb5`, against pinned CLI `0b896ef`, passed type checking and linking, and both general and CFW help commands ran successfully. The cache contribution changes setup scripts and leaves those CLI sources unchanged. The connection helper also accepted a real research bundle's configuration and matched its ECID file.

The transport checks exercise FUSE and OpenSSH with local stubs under Bash and `/bin/sh`; they do not mount a guest or establish a complete CFW installation. The locally linked CLI has machine-specific library paths and is a verification artifact, not a portable binary release. Upstream workflows require maintainer approval.

Upstream contributions use the projects' own interfaces and terminology. The backend's public controls use `VPHONE_*`, including private USB readiness, first-boot QMP, timeouts and optional transport tracing. Limulator translates its compatibility inputs locally; upstream source does not need Limulator-specific environment variables. The integration branch combines component changes, while each upstream PR explains its own failure trigger, impact, and validation limits.

## Fixes already upstream

The acquire/release (`0006`), jump-cache (`0008`) and generic-timer (`0014`) changes came from QEMU upstream commits [38c32a9](https://github.com/YahiaAngelo/qemu-vphone/commit/38c32a9bf16d92c0fd1d82b20c051cbe34a90372), [f6b3bc9](https://github.com/YahiaAngelo/qemu-vphone/commit/f6b3bc9d8ec9ba3113a4dbae9e9584775cd0d1f2), and [3e884e8](https://github.com/YahiaAngelo/qemu-vphone/commit/3e884e825cd6d545f9774c236113cfd8745e204e). The QEMU fork retains these credited backports on the tested older base. No duplicate PR is needed, and moving to the entire latest upstream tree would require broader validation.

The original libirecovery project fixed the recovery-client lifetime issue in [96b150b](https://github.com/libimobiledevice/libirecovery/commit/96b150b5c9b215f1ccb1eff31bd60cc84c9417d3). The CLI contribution updates its older bundled copy. The native CLI already permits an incremental firmware update without the original restore tree; the Linux overlay needs its corresponding adapter change.

## Limulator acceptance policy

The former first-boot migration gate (`0018`) is retained on the backend fork's `dev` branch as a Limulator integration requirement. It requires a matching firmware build, Boolean `data_migration_done: true`, and a successful result from the private `com.apple.migration` settings before changing setup state. Both Boolean `success: true` and integer `success: 1` are accepted; Boolean false and integer zero are failures. Strings, floats, and other values do not permit setup to continue. Its parser and shell behavior have fixture coverage. This does not establish that Apple's private fields have the same meaning across OS releases, or that an explicit failure always indicates a terminal condition.

This policy has no upstream bug-fix PR. Keep its runtime acceptance evidence separate from generic launcher, transport and cache-publication fixes. The existing diagnostic iPhone guest is useful for app interaction but does not satisfy this stricter clean-creation requirement.

## Updating an existing installation

An existing backend may contain the former locally applied patches. Bootstrap refuses tracked or staged source modifications, even if they resemble an old Limulator patch. It preserves those files and build artifacts. It does not reset a checkout, discard a patch, restore a guest, or rewrite a running backend.

1. Keep your current backend and guest data. Do not change files used by a running guest. Finish the guest session before switching which backend it will use.
2. Pull Limulator without discarding local work, then run `uv sync --locked`.
3. If bootstrap reports modified sources, select a new backend directory. Keep your existing `LIMULATOR_HOME`, `LIMULATOR_PREFIX` and `LIMULATOR_CACHE` settings so that device registrations, tools, SDKs, firmware and downloads remain available.

```sh
export LIMULATOR_BACKEND="$HOME/.local/share/limulator/backends/vphone-linux-dev"
uv run limulator bootstrap sources
```

The source step clones or fetches the exact lock revisions, including the QEMU fork even when the Linux backend's upstream gitlink names another revision. It does not compile anything or start a guest. On a new backend directory, build the backend components once:

```sh
LIMULATOR_JOBS=4 uv run limulator bootstrap all
uv run limulator doctor
```

This reuses existing downloads and tools where their receipts permit. A new source directory still needs its own native build products. It does not require a new iOS restore or another xtool SDK installation. Keep the selected `LIMULATOR_BACKEND` in your normal local environment configuration before starting an existing registered device.

On later clean source updates, run `bootstrap sources` and rebuild only the changed components, such as `bootstrap qemu` or `bootstrap cli`. Preserve untracked and ignored build output unless the component's build system explicitly requires a clean build. Follow [the app workflow](APP_WORKFLOW.md) for everyday app changes; they do not require a backend update.

To verify an existing source checkout without changing it:

```sh
uv run tools/sync_backend_sources.py \
  --lock third_party.lock.json \
  --backend "$LIMULATOR_BACKEND" \
  --check
```

## Maintaining the forks

1. Reproduce a dependency defect in that component's checkout. Keep app workflows and Limulator policy changes in this repository.
2. Start a focused contribution branch from the current upstream default branch. Check upstream history first; credit and reuse existing fixes instead of submitting duplicate backports.
3. Keep independent changes in independent PRs. If one requires another, state the dependency and choose a base that makes the relationship reviewable.
4. Validate the changed behavior with the component's relevant tests, build or smoke checks. Record the commands, results and missing runtime evidence in the PR. Preserve authorship, copyright notices and signed task commits.
5. Integrate the reviewed commits on the fork's `dev` branch and validate the combined dependency sources. Update `third_party.lock.json` only to the tested full commit identifiers. A branch name alone is not a reproducible dependency.
6. When upstream merges a fix, use the upstream implementation and remove the corresponding fork-only delta. Revalidate before advancing the lock. Do not keep a patch file as a second implementation.

Upstream CI can require a maintainer to approve a fork's workflow run. An empty PR check list does not mean CI passed; inspect the linked Actions run for `action_required`. Record local validation separately and leave approval to the upstream project.

Source checkout tests, native compilation and trace registration establish their respective layers. They do not establish clean guest creation, interrupt handling, app performance, Apple device parity or App Store publication. Keep those acceptance results separate.
