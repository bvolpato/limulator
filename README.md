# Limulator

Run an iPhone app built with xtool on a local Linux workstation. Install the app into a real iOS guest, interact with it, capture its screen, and run repeatable checks from the terminal or a local web console.

Development status: CounterLab's iPhone 16 profile and functional checks pass in a real iOS 27.0.1 guest, including native scrolling, taps, Unicode input, navigation, and count persistence after app relaunch. A warm Release build with SDK 27.2 took 3.03 seconds. A separate closed-Duo trial accepted BruTV login input, rendered advancing HLS fixture video and resumed video after an eleven-minute pause. Broader playback stability remains unverified. Clean creation is not accepted: DataMigrator still reports `success: false` for build `24A446`. App performance, repeatable cold startup, and complete Duo/iPad acceptance remain unverified. See the [current native results](docs/APP_WORKFLOW.md#current-iphone-16-result) for scope and evidence limits.

Limulator uses an emulated ARM64 iPhone. UIKit and SwiftUI run inside iOS; the console displays frames from that guest. A remote Mac and replacement web UI are not the execution path.

The selected backend is [vphone-linux](https://github.com/YahiaAngelo/vphone-linux), pinned with its QEMU, Reims vGPU, and Metal-to-Vulkan dependencies. The upstream project reports iOS 27.0.1 and iPhone Duo/iOS 27.1 on x86 Linux. Those reports are inputs to our validation, not proof that this workstation works.

Limulator keeps that backend as a separate source dependency. [The lock file](third_party.lock.json) pins complete commits in our dependency forks, rather than applying a local patch series during installation. [Upstream contributions](docs/UPSTREAM.md) records the ownership of each change, its pull request, and the process for returning to upstream revisions. Limulator owns the build workflow, device management, console, and acceptance checks.

New guest sessions use Reims vGPU's normal compatible policy by default. Strict checks and census-backed diagnostic mode are available as explicit choices; none establishes equivalence with a physical Apple GPU.

For the normal development loop, use [Build, deploy, and test apps](docs/APP_WORKFLOW.md). It reuses an existing guest and separates app updates from the one-time firmware and tool setup. See [the setup guide](docs/SETUP.md) for installation, and [the research](docs/RESEARCH.md) and [architecture](docs/ARCHITECTURE.md) for the decision, acceptance criteria, and fidelity limits.

## Development

```sh
uv sync
uv run limulator --help
uv run pytest
uv run ruff check .
```

VM disks, firmware, SDKs, apps, and private keys are local data and are excluded from Git. The website includes four reviewed CounterLab captures from compatible-graphics iPhone 16 runs, an older diagnostic BruTV frame, and one closed-Duo capture of a local fixture playback recovery trial; each image records the specific observation it supports. These captures do not establish complete native acceptance. Limulator does not redistribute Apple firmware. Locally built ARM64 iPhone apps are the primary input; support for arbitrary App Store binaries is not claimed.

## Credits

The execution stack is the work of vphone-linux, vphone-cli, ChefKiss/QEMU, Reims vGPU, and metal2vulkan contributors. Limulator supplies the Linux workflow, lifecycle management, console, and test interfaces around those components. Each dependency retains its own license; see [the upstream license matrix](https://github.com/YahiaAngelo/vphone-linux/blob/main/docs/LICENSING.md).

## License

Limulator's original code is available under the [MIT License](LICENSE). Dependencies and derived benchmark fixtures retain their upstream licenses and notices, listed in [the third-party notices](THIRD_PARTY_NOTICES.md). Apple firmware, SDKs, and guest disks are not included.
