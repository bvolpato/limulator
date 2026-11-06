# Limulator

Run an iPhone app built with xtool on a local Linux workstation. Install the app into a real iOS guest, interact with it, capture its screen, and run repeatable checks from the terminal or a local web console.

Development status: app compilation, host protocols, lifecycle safeguards, and the console have local validation. The iOS 27.1 restore and research guest installation are complete; normal first boot and data migration are in progress. The guest health endpoint reports setup pending, while device metadata has not yet returned. Native app execution, input, persistence, software postures, app performance, and usable guest screenshots remain unverified.

Limulator uses an emulated ARM64 iPhone. UIKit and SwiftUI run inside iOS; the console displays frames from that guest. A remote Mac and replacement web UI are not the execution path.

The selected backend is [vphone-linux](https://github.com/YahiaAngelo/vphone-linux), pinned with its QEMU, Reims vGPU, and Metal-to-Vulkan dependencies. The upstream project reports iOS 27.0.1 and iPhone Duo/iOS 27.1 on x86 Linux. Those reports are inputs to our validation, not proof that this workstation works.

Limulator keeps that backend as a separate source dependency. [The lock file](third_party.lock.json) pins its revisions and the checksums of our [reviewed patches](patches/). Emulator and guest-component fixes should be developed in focused forks and contributed upstream; Limulator owns the build workflow, device management, console, and acceptance checks. Copying the emulator into this repository would duplicate its maintenance and make upstream updates harder to review.

See [the setup guide](docs/SETUP.md) for installation and app commands, and [the research](docs/RESEARCH.md) and [architecture](docs/ARCHITECTURE.md) for the decision, acceptance criteria, and fidelity limits.

## Development

```sh
uv sync
uv run limulator --help
uv run pytest
uv run ruff check .
```

VM disks, firmware, SDKs, apps, private keys, and screenshots are local data and are excluded from Git. Limulator does not redistribute Apple firmware. Locally built ARM64 iPhone apps are the primary input; support for arbitrary App Store binaries is not claimed.

## Credits

The execution stack is the work of vphone-linux, vphone-cli, ChefKiss/QEMU, Reims vGPU, and metal2vulkan contributors. Limulator supplies the Linux workflow, lifecycle management, console, and test interfaces around those components. Each dependency retains its own license; see [the upstream license matrix](https://github.com/YahiaAngelo/vphone-linux/blob/main/docs/LICENSING.md).

## License

Limulator's original code is available under the [MIT License](LICENSE). Derived patches and the SMC benchmark retain their upstream licenses and notices, listed in [the third-party notices](THIRD_PARTY_NOTICES.md). Apple firmware, SDKs, and guest disks are not included.
