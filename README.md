# Limulator

Run an iPhone app built with xtool on a local Linux workstation. Install the app into a real iOS guest, interact with it, capture its screen, and run repeatable checks from the terminal or a local web console.

Development status: app compilation, host protocols, lifecycle safeguards, and the console have local validation. A diagnostic iPhone 16 guest running iOS 27.0.1 has rendered BruTV, with the app verified as frontmost. Console unlock, launch, and a coordinate tap focusing its empty server-address field have been observed. That guest still reports DataMigrator `success: false` for build `24A446`, so clean creation and release acceptance remain blocked. Repeatable startup, text entry, accessibility control automation, persistence, playback, software postures, app performance, and Duo/iPad runtime acceptance remain unverified.

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

VM disks, firmware, SDKs, apps, and private keys are local data and are excluded from Git. The website includes one reviewed diagnostic guest screenshot. Limulator does not redistribute Apple firmware. Locally built ARM64 iPhone apps are the primary input; support for arbitrary App Store binaries is not claimed.

## Credits

The execution stack is the work of vphone-linux, vphone-cli, ChefKiss/QEMU, Reims vGPU, and metal2vulkan contributors. Limulator supplies the Linux workflow, lifecycle management, console, and test interfaces around those components. Each dependency retains its own license; see [the upstream license matrix](https://github.com/YahiaAngelo/vphone-linux/blob/main/docs/LICENSING.md).

## License

Limulator's original code is available under the [MIT License](LICENSE). Dependencies and derived benchmark fixtures retain their upstream licenses and notices, listed in [the third-party notices](THIRD_PARTY_NOTICES.md). Apple firmware, SDKs, and guest disks are not included.
