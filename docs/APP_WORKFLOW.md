# Build, deploy, and test apps

Use an existing registered guest for day-to-day app development. Firmware downloads, `bootstrap all`, and `create` belong to the [one-time setup](SETUP.md). Installing another app does not require another iOS restore.

Here, deploying means installing a development app into the local research guest. Store publication has a separate signing and upload workflow.

## Select the existing guest

Run these commands from the Limulator checkout:

```sh
uv run limulator devices
limulator_device=phone
uv run limulator --device "$limulator_device" status
```

Replace `phone` with a name returned by `devices`, not a profile name from `profiles`. Always select the same data directory that contains that registration. The default is `~/.local/share/limulator`.

If your guests and shared tools use different locations, configure them once in the terminal:

```sh
export LIMULATOR_HOME=/absolute/path/to/existing/limulator-data
export LIMULATOR_BACKEND=/absolute/path/to/shared/backends/vphone-linux
export LIMULATOR_PREFIX=/absolute/path/to/shared/tools
uv run limulator devices
```

`--data` also selects the data directory. A custom data directory changes the default tool paths too, so retain `LIMULATOR_BACKEND` and `LIMULATOR_PREFIX` when sharing an existing tool installation.

The selected xtool wrapper keeps using its installed Swift toolchain and SDK when the guest data directory changes. `LIMULATOR_XTOOL_CONFIG` remains available for an explicit SDK configuration override. A new guest directory does not require another Swift or SDK installation.

For repeated use, those three assignments can live in an ignored `.limulator/dev.env` file. Use `uv run --env-file .limulator/dev.env limulator ...` instead of exporting them in every terminal. Keep local paths, VM disks, SDKs, and app artifacts out of Git.

## Select a graphics policy

Compatible mode is the default for new launches. It follows Reims vGPU's normal policy, which allows ordinary precision differences while retaining its structural failure checks. Select `--graphics-mode strict` on `create`, `start`, or `run` when you want Reims's stricter failure policy. To inventory differences, select `--graphics-mode diagnostic` or set `LIMULATOR_GRAPHICS_MODE=diagnostic` for commands without the option. The command-line option takes precedence. Diagnostic mode enables a private census and may continue through broader differences; use it for exploration, not acceptance. Keep stderr notices with run evidence. No mode establishes full Metal equivalence. The [setup guide](SETUP.md#choose-a-graphics-policy) describes these limits.

`run` reuses a running guest when no mode is selected. If you request a different mode, Limulator rejects the request without restarting the guest; stop it and start a new session with the chosen mode. Runtime records created before mode tracking report `graphics_policy.mode: unknown`; Limulator does not infer the old mode from the current environment. An explicit mode on `run` is checked before the app build begins.

## Open the native window once per session

When the selected guest is stopped:

```sh
uv run limulator --device "$limulator_device" start --window --wait 300
```

Leave that window running while changing apps. `run` reuses a running guest. If the guest is stopped, `run` starts it headlessly; start it with `--window` first when you want the desktop UI.

The right-side toolbar provides Home, Power, volume, and Screenshot controls. Home wakes a locked display and performs an upward unlock gesture; on an unlocked device it sends Home. Power wakes a sleeping display or sends the virtual side-button action. Passcodes require your input. Screenshot saves only when you click it.

Interactive windows keep their working memory out of swap. They retain the full profile resolution. The window stays open until you close or stop it; there is no one-hour application limit.

## Build and deploy a source project

For a SwiftPM iOS project configured for xtool:

```sh
uv run limulator --device "$limulator_device" run /absolute/path/to/MyApp
```

This builds a Debug IPA, starts the guest only if needed, installs the app, unlocks the screen, and launches the installed bundle. Keep the project's build cache for incremental builds.

The included project is a working template:

```sh
uv run limulator --device "$limulator_device" run examples/CounterLab
```

Its bundle ID is `com.bvolpato.limulator.counterlab`. When copying the template for another app, update `xtool.yml`, the package, and its Info.plist for that app.

To build without changing the running guest:

```sh
uv run limulator build /absolute/path/to/MyApp --configuration debug
```

Use `--configuration release` for a Release app build. The command reports the generated artifact and inspected app metadata. An existing Xcode project can use its own build pipeline, then deploy the resulting device artifact through `install`.

## Deploy an existing artifact

Skip the source build when you already have a development `.app` or `.ipa`:

```sh
uv run limulator --device "$limulator_device" install /absolute/path/to/MyApp.ipa
uv run limulator --device "$limulator_device" unlock
uv run limulator --device "$limulator_device" launch com.example.MyApp
```

Replace the bundle ID with the one reported by `install`. Device ARM64 bundles are accepted; Apple Simulator binaries and encrypted App Store apps are rejected. Guest installation signs for this research environment; it does not validate store distribution signing.

If the app is already installed, use only `unlock` and `launch`. The separate `launch` command does not wake a locked guest.

## Inspect and test

For manual interaction, use the native window or open the local console:

```sh
uv run limulator --device "$limulator_device" serve
# Open http://127.0.0.1:18765
```

For a screenshot, choose a new output filename:

```sh
mkdir -p artifacts
uv run limulator --device "$limulator_device" screenshot artifacts/my-app.png
```

After installing CounterLab, its smoke scenario exercises counter changes, text entry, navigation, and persistence after relaunch:

```sh
uv run limulator --device "$limulator_device" unlock
uv run limulator --device "$limulator_device" test \
  examples/CounterLab/scenarios/smoke.json \
  --output artifacts/counterlab-smoke-01
```

Each test run needs a new output directory. It writes `report.json` with the recorded graphics policy and guest screenshots. Adapt the JSON scenario and accessibility identifiers for your app. The [setup guide](SETUP.md#run-app-checks) explains scenario actions and their limits. The existence of a scenario does not establish that it has passed on every profile, and a functional pass does not establish full Metal equivalence.

### Current iPhone 16 result

On 7 November 2026, a fresh iPhone 16 guest running iOS 27.0.1, build `24A446`, completed ordinary Setup Assistant through the UI without editing setup or migration markers. DataMigrator still reports `success: false`; the host creation receipt is unchanged and no booted marker is present. This is not accepted clean creation.

A compatible-graphics run had `REIMS_VGPU_STRICT=0`, with `REIMS_VGPU_CENSUS_DIR` and `REIMS_VGPU_EXACT` unset in the actual QEMU environment. One guest reboot reached API ready with CounterLab count `1` retained. A targeted semantic tap on the single visible `counterlab.increment` element returned `tapped: true` and changed the count to `2`. Native UIKit metrics and one read-only HTTPS request to `example.com` returning HTTP 200 were also observed.

A controlled 12-step run of `iphone-16.json` passed its OS version, UIKit idiom, point-size, and scale checks. A separate 26-step run of `smoke.json` passed in compatible graphics. It covered reset and increment, semantic scrolling and taps, Details navigation, Unicode text input (`Hello from Linux ✓`), Home, and count `1` after app terminate/relaunch. The note uses transient state; its persistence was not tested. The run took 52.59 seconds, which is scenario duration, not an app performance measurement. App-process persistence passed once; repeatable cold guest startup remains unverified. Playback, app performance, and Duo/iPad behavior remain unverified.

CounterLab also displays its native OS version and active UIKit screen metrics. On an iPhone 16 guest in portrait, check those values separately:

```sh
uv run limulator --device iphone-16 test \
  examples/CounterLab/scenarios/iphone-16.json \
  --output artifacts/iphone-16-profile-01
```

Use your registered device name in the command. For the iPad mini profile, select `examples/CounterLab/scenarios/ipad-mini.json`; it requires the native `iPad` interface idiom and a 744 by 1133 point screen at 2x scale. These assertions compare app readback with the expected profile. They do not verify a corresponding physical Apple GPU, and their presence does not establish a successful native run.

### Current Duo result

On 8 November 2026 UTC, an existing iPhone Duo research guest running iOS 27.1, build `24A94425`, ran CounterLab in compatible graphics. The cover reported 466 by 678 interface points at 3x scale. Book and open postures produced a real two-column UIKit layout reporting 951 by 669 points at 3x scale. The guest reported settled hinge angles of 100, 180, and 0 degrees during the round trip. The presented framebuffer remained the single 1398 by 2034 panel, rotated when open; this does not establish a second physical scanout.

Screenshot-coordinate input on the open display changed the count from `0` to `1`, and returning to the cover retained `1`. A semantic tap on the cover then changed it to `2`. Inner-display accessibility input is blocked: `ui.tap_element` returned `tapped: true` for Add one but left the count at `0`. The guest's `device.screen` still reported 678 by 466 points while the app rendered at 951 by 669, and the flat AX snapshot omitted visible lower controls. The pinned IcliKit input path uses the main-panel geometry rather than the active inner viewport. Removing Limulator's bounds checks would not correct that touch transform.

These observations used backend `363c82a`, CLI `0b896ef`, and QEMU `fa07541`. The newer toolbar revision fixes cold Fold discovery separately and has regression and fork CI validation. The full Duo scenario has not passed, clean creation remains unaccepted, and inner-display geometry and semantic input require a shared guest-side fix before claiming complete Duo support.

A later isolated-clone trial used only the FoldDisplay backports from merged upstream [CLI #788](https://github.com/Lakr233/vphone-cli/pull/788) and [#799](https://github.com/Lakr233/vphone-cli/pull/799), built at [fork commit `ec2cbb1`](https://github.com/bvolpato/vphone-cli/commit/ec2cbb1060b0504fbccc44d58d04521c1dfef221). After a cold boot, the inner CADisplay reported built-in type `0` and point scale `3`, replacing virtual type `3` and scale `1`. CounterLab still rendered the real 951 by 669 point layout at 3x, and the cover counter check passed. The update changed only the FoldDisplay library, and the root filesystem returned to read-only after reboot.

Those metadata corrections did not resolve inner semantic input. In stable book posture, Add one's accessibility tap returned `tapped: true`; a later independent AX read still showed count `1`. The daemon continued reporting cover geometry of 678 by 466 points. The full scenario also exposed a separate transition issue: hinge settlement can precede a responsive SpringBoard orientation read. Waiting for fresh hinge evidence and allowing a bounded post-fold orientation read then passed a native closed-to-book capture scenario in 27.70 seconds. That is capture validation, not an input or performance pass. This trial does not advance the default CLI pin or establish complete Duo support.

## Shortest path for each change

| What changed | What to run |
|---|---|
| App source | `run /path/to/project` |
| Prebuilt app bundle | `install /path/to/app`, then `unlock` and `launch` |
| Nothing; reopen an installed app | `unlock`, then `launch bundle.id` |
| Guest is stopped | `start --window`, then use the app commands |
| Need a different iOS device or OS | Follow the one-time profile creation guide |

## Recover without repeating setup

- **No registered device:** check `LIMULATOR_HOME` or `--data` before creating another guest. A different data directory has a different registry.
- **Running without a native window:** stop it, then `start --window`. A native window cannot be attached to a guest that started headlessly.
- **Screen locks while idle:** press Home in the native toolbar or run `unlock`, then `launch` the installed app. Unlock uses a bounded touch fallback for the guest's known no-passcode Home timeout. Enter any passcode locally.
- **App install or launch fails:** retain the error and the existing guest. Recreating iOS is not the normal app-update recovery step.
- **First setup or migration fails:** preserve that guest and its logs and use the creation recovery instructions in [SETUP.md](SETUP.md). The app workflow does not repair a failed OS migration.

Stop the guest when finished:

```sh
uv run limulator --device "$limulator_device" stop
```

For a repository update, stop the guest first, pull without discarding local work, and run `uv sync --locked`. When dependency revisions change, follow [the source migration instructions](UPSTREAM.md#updating-an-existing-installation) and rebuild only the affected backend component. Re-running `bootstrap all`, deleting build caches, or restoring the VM for each app change adds unnecessary work.

Clean guest creation, repeatable cold guest startup, playback, app performance, Duo/iPad behavior, and distribution readiness remain subject to the limits in the [README](../README.md). A reusable research guest does not establish these results.
