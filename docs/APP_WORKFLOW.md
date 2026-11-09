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

Creating a new guest also uses the SDK to build the ECID-aware SSH ramdisk bootstrap. With shared tools and a separate guest data directory, set `LIMULATOR_XTOOL_CONFIG` to the shared installation's `xtool/config` directory. `DARWIN_SDK_BUNDLE` can select a bundle directly. Alternatively, `VPHONE_SSHRD_BOOTSTRAP` accepts a trusted prebuilt bootstrap; the backend validates its architecture and signed code before staging it.

For repeated use, these assignments can live in an ignored `.limulator/dev.env` file. Use `uv run --env-file .limulator/dev.env limulator ...` instead of exporting them in every terminal. Keep local paths, VM disks, SDKs, and app artifacts out of Git.

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

A controlled follow-up used CounterLab's own reported 951 by 669 point screen to normalize the Add one AX point into the daemon's 678 by 466 point coordinate space. That tap changed the native count from `1` to `2`. This supports the normalization mismatch as the input defect. It did not change the semantic API: arbitrary apps still need a trustworthy active-display geometry source, rather than dimensions inferred from their controls or supplied by a test app.

A later experimental worker used native FrontBoard geometry and a scoped Icli backport. An inner-display semantic tap changed CounterLab's count from `2` to `3`. A fresh BruTV device build using SDK 27.2 was also installed and launched as a user app in the isolated research guest. Its login screen appeared in a raw native inner-display capture at 2007 by 2853 pixels. The capture used matching display identity, scale and bounds before and after rendering; it applied no resize or rotation. The resulting image was sideways, so orientation mapping remains unverified.

At that stage, BruTV's M3U tab did not change after either semantic input or a controlled cover-display QMP tap. A successful dispatch response therefore did not establish delivery to the app. Moving-video playback, repeatable startup of the default bundle, clean iPad creation and the full Duo scenario remain unverified. These experiments do not advance the default dependency pins or establish store distribution readiness. See [the separate upstream proposals](UPSTREAM.md).

A subsequent worker update and one clean guest shutdown and cold start retained the new worker's exact hash and confirmed its cache marker. This tests that experimental cached update path, not repeatable startup for the default bundle. During separate bounded observations of one QMP tap and one guest-native tap, neither BruTV nor SpringBoard reported a call to `UIApplication.sendEvent:`. The native device list lacked the virtual USB touchscreen, and the serial log reported HID-driver launch failures.

The next trial changed only the personalized Preboot device tree's `/product/supports-driverkit` property. Host-copy checks preserved all non-payload IMG4 records, and the atomic guest write passed hash, ownership and permission readback. After a clean cold boot, the unchanged experimental worker reported the expected hash and the guest registered its USB touchscreen and keyboard. BruTV launched as the foreground app. A semantic tap changed its form to `PLAYLIST URL` in 1.89 seconds; physical QEMU taps then switched to `SERVER ADDRESS` and back in 1.57 and 1.86 seconds. Native text entry filled the exact local fixture URL, and Load Playlist reached BruTV Home. This is direct input-recovery evidence for selected controls on the closed Duo display. Inner-display orientation, all posture transitions, viewport fidelity and arbitrary controls still need their own native checks.

A separate native file-download regression located a 64 KiB VSOCK boundary failure. The same generated files that timed out before the accepted-socket send-buffer fix downloaded with matching full hashes afterward, along with the original control files and the complete cached worker. The change covers accepted HTTP, RPC and upgraded-stream sockets; its successful warm-cache download does not establish general application or network throughput.

### Closed-Duo live video trial

The same isolated guest loaded BruTV's local HLS fixture and opened its live channel. Two native captures showed the video's source counter advancing by 238.87 seconds across 238.82 seconds of host time. The video region changed independently of the OS clock, and the fixture reported no missing segments. This proves advancing rendered H.264 fixture video in one closed-Duo session. It does not measure continuous frame rate, audio fidelity or general app performance.

A later long pause followed by Play left the player buffering without video. BruTV's existing local playback-health archive recorded startup time, a stall and retries. The archive is local data; this trial added no customer analytics upload. A focused app regression subsequently found that expired local recording segments returned HTTP 410 but were treated as terminal failures. The app fix makes that local response retryable while keeping provider 404/410 responses terminal. The original stall's cause is not yet established.

BruTV commit `d62c6b1` was then built with SDK 27.2 and its installed executable UUID was verified. In a fresh unlocked closed-Duo trial, the header used its two-row fallback and the app reported the expected 466 by 678 point viewport without the former horizontal overflow. Native captures showed the title, status and action controls within the viewport, with controls visible in the three-second capture after a physical tap. This validates the narrower header in one session; it is not a responsiveness benchmark or a complete posture check.

With that build, a physical Play tap after a 666.58-second pause restored `playing video` in 3.28 seconds. The source counter advanced in subsequent native captures, including one over a minute later. The trial left Auto-Lock at 300 seconds and used periodic control-only taps to keep the display awake. During the pause, the displayed frame moved forward after its position aged out of the ten-minute recording window. Source inspection confirms that the existing retention policy seeks a paused item to the oldest retained point without playing it. This is one successful long-pause recovery trial; it does not isolate HTTP 410 as the original stall's cause or prove every network and lifecycle case.

The combined `fa4c3b0a` bundle's worker was subsequently installed and its exact executable hash and confirmed cache marker were read back. Health, active geometry, foreground-app identification and BruTV accessibility inspection passed in the same closed-Duo session. Active-display capture refused the cover panel because its native identity lacks a hardware UUID; QEMU captures remain available. The worker also reports a pending MobileGestalt restart. This hot-update smoke check does not establish cold-start persistence or full acceptance of the combined bundle.

The later `f922fdc` app build displays existing live-buffer notices, including the expiry explanation. Its Release build and full iPhone, iPad and Duo CI suites pass. That exact app was installed and its executable UUID verified in the research guest, where fixture video rendered. Actual expiry-notice appearance has not yet been observed natively.

Candidate `5caaea8d` backports the cover-capture fix to the combined bundle. Its complete package workflow passes, including Linux resource packaging and backend extraction. After a guarded worker update, a full cached-executable download matched the package and the pending marker was absent. One warm native capture then produced a 1398 by 2034 pixel PNG with matching display metadata before and after rendering, no resize and no rotation. The first capture exceeded its native response deadline, and a separate foreground-app query timed out. This verifies one warm cover capture without a hardware UUID; cold capture, startup persistence, other postures and general API responsiveness remain open. The default pins remain unchanged.

A subsequent clean shutdown and cold boot did not return the guest API and remained at the Apple logo. The cause is unresolved. During later diagnosis, an HMP instruction dump hit QEMU's existing unsupported-disassembly assertion and aborted the host process. That is a separate defect, now covered by [qemu-vphone #4](https://github.com/YahiaAngelo/qemu-vphone/pull/4) and a passing executable regression. The capture proposal remains draft. The failed disk, NVRAM and SEP storage were preserved together and hash-verified into an isolated recovery copy; this does not establish a successful cold boot.

An interrupted diagnostic copy subsequently reached Home using a virtual keyboard wake followed by a touchscreen swipe. Its cached agent remained alive, but port 1339 rejected connections before completing the VSOCK handshake. Local USB pairing, log collection and a guarded thread sample worked without the agent API. The UUID-matched sample showed completed startup and idle network loops. A kernel socket inventory then reported the API listener as disconnected and closed for sending and receiving, while the same worker's camera port still accepted a connection. The operation that caused this state remains unidentified.

One guarded agent restart restored orientation and health requests on that same iOS boot, without restarting QEMU or changing cache markers. A supervised script and a signed read-only native observer also ran successfully. The observer confirmed the replacement's stable process identity and healthy API listener. This establishes recovery and a baseline for further diagnosis; it does not repair the original failed boot or establish repeatable startup. [The component fixes and their limits](UPSTREAM.md) remain separate from cold-start acceptance.

### iPad display-classification recovery

An isolated iPad mini guest exposed a mismatch between its 326 PPI profile and the framebuffer's 95 PPI readback. A focused QEMU backport and launcher change corrected the native framebuffer to 326 PPI without changing its 1488 by 2266 pixels or scale 2. MobileGestalt retained the old answer until its generated cache was archived, removed using the existing display-change procedure, and the guest was cleanly shut down and booted. ProgressUI's native environment then reported supported screen class 28 instead of -1. The guest API was observed responsive about ten minutes after this cache-refresh boot. Fresh creation, migration success and CounterLab's iPad scenario remain unaccepted. See [the component fixes and evidence limits](UPSTREAM.md).

A later fresh iPad mini trial with the corrected density completed stock restore and CFW installation. Native migration results report `success: true` for the expected build `24A446`, with `data_migration_done: true` and setup no longer pending. Final host verification then timed out reading HTTP headers, leaving an error receipt. Boot verification was resumed from the same disk, without repeating restore or CFW. Its first health producer took minutes to complete while file reads and setup queries already worked; later health returned in about 0.2 seconds with the expected OS and installed executable hash. The backend then rewrote scale 2 to the same value and forced another boot, which missed its ten-minute readiness bound and was stopped by cleanup. No ready receipt or CounterLab scenario pass was produced.

[vphone-linux #54](https://github.com/YahiaAngelo/vphone-linux/pull/54) removes that unnecessary write and reboot only when full tree bytes and running scale already agree. Its regression and upstream CI pass; it still needs native validation. Limulator now records the individual setup, migration, unlock and device-info verification stages so a later timeout identifies the blocked request. Migration success is a verified milestone, while complete creation, repeatable startup and the iPad app scenario remain unaccepted.

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
