# Third-party notices

The [MIT license](LICENSE) covers Limulator's original code. Dependencies and derived fixtures retain their upstream notices and licenses. The copies in `LICENSES/` reproduce upstream texts without changing those terms.

[third_party.lock.json](third_party.lock.json) identifies the separately fetched dependency sources and immutable revisions. Limulator no longer distributes or applies its former dependency patch series. [Upstream contributions](docs/UPSTREAM.md) records the replacement fork commits and pull requests.

## vphone-linux

The Linux backend is maintained in [bvolpato/vphone-linux](https://github.com/bvolpato/vphone-linux), a fork of [YahiaAngelo/vphone-linux](https://github.com/YahiaAngelo/vphone-linux). The migration preserves the source changes originally based on commit `c2dc1996c8398ddb994288a29a719360f354d268`. The backend's own Linux source overlays and APFS compatibility patch remain part of that dependency.

Its notice is `Copyright (c) 2026 Yahia`, under the MIT License. The complete notice, permission text, disclaimer, and repository-scope statement are retained in [LICENSES/vphone-linux-MIT.txt](LICENSES/vphone-linux-MIT.txt), copied from the [original pinned LICENSE](https://github.com/YahiaAngelo/vphone-linux/blob/c2dc1996c8398ddb994288a29a719360f354d268/LICENSE). The derived first-boot shell fixture under `tests/fixtures/migration-first-boot` retains these terms.

## vphone-cli

The native CLI is maintained in [bvolpato/vphone-cli](https://github.com/bvolpato/vphone-cli), a fork of [Lakr233/vphone-cli](https://github.com/Lakr233/vphone-cli). The migrated dyld-cache changes were based on commit `83197152137ddd2aad70eb121d6b88a9afbee36e`.

The project notice is `Copyright (c) 2026 @Lakr233`, under the MIT License. The complete text is retained in [LICENSES/vphone-cli-MIT.txt](LICENSES/vphone-cli-MIT.txt), copied from the [original pinned LICENSE](https://github.com/Lakr233/vphone-cli/blob/83197152137ddd2aad70eb121d6b88a9afbee36e/LICENSE). Linux overlays retain this project notice and the vphone-linux MIT notice above.

## QEMU

The emulator is maintained in [bvolpato/qemu-vphone](https://github.com/bvolpato/qemu-vphone), a fork of [YahiaAngelo/qemu-vphone](https://github.com/YahiaAngelo/qemu-vphone). The migration starts from commit `14c9913d2231080bd234c69421ed4f293a25f767`.

The acquire/release ordering and jump-cache invalidation implementations credit upstream commits [38c32a9](https://github.com/YahiaAngelo/qemu-vphone/commit/38c32a9bf16d92c0fd1d82b20c051cbe34a90372) and [f6b3bc9](https://github.com/YahiaAngelo/qemu-vphone/commit/f6b3bc9d8ec9ba3113a4dbae9e9584775cd0d1f2). Exact generic-timer conversion credits upstream commit [3e884e8](https://github.com/YahiaAngelo/qemu-vphone/commit/3e884e825cd6d545f9774c236113cfd8745e204e). Those upstream changes are not original Limulator code.

The complete pinned QEMU license texts are retained in [LICENSES/qemu-GPL-2.0.txt](LICENSES/qemu-GPL-2.0.txt) and [LICENSES/qemu-LGPL-2.1.txt](LICENSES/qemu-LGPL-2.1.txt). Source files retain their individual headers and copyright holders.

The fork's [original pinned LICENSE](https://github.com/YahiaAngelo/qemu-vphone/blob/14c9913d2231080bd234c69421ed4f293a25f767/LICENSE) records the ChefKissInc/QEMUAppleSilicon provenance, its project-level GPL version 3 and own-parts AGPL version 3 statements, and the original QEMU license notice. That full notice is retained in [LICENSES/qemu-NOTICE.txt](LICENSES/qemu-NOTICE.txt), with the accompanying [GPL version 3](LICENSES/qemu-GPL-3.0.txt) and [AGPL version 3](LICENSES/qemu-AGPL-3.0.txt) texts. File-specific notices remain distinct.

## libirecovery

The recovery-client change updates the [libirecovery source bundled in vphone-cli](https://github.com/Lakr233/vphone-cli/blob/83197152137ddd2aad70eb121d6b88a9afbee36e/VPhoneExecutable/VPhoneCommand/VPhoneRestore/MobileRecoveryCore/libirecovery.c). The original project already corrected this lifetime issue in [96b150b](https://github.com/libimobiledevice/libirecovery/commit/96b150b5c9b215f1ccb1eff31bd60cc84c9417d3); the contribution to the CLI is a backport. Its retained header lists:

- `Copyright (c) 2011-2023 Nikias Bassen <nikias@gmx.li>`
- `Copyright (c) 2012-2020 Martin Szulecki <martin.szulecki@libimobiledevice.org>`
- `Copyright (c) 2010 Chronic-Dev Team`
- `Copyright (c) 2010 Joshua Hill`
- `Copyright (c) 2008-2011 Nicolas Haunold`

The header states GNU Lesser General Public License version 2.1 and retains its warranty disclaimer. The complete bundled [COPYING](https://github.com/Lakr233/vphone-cli/blob/83197152137ddd2aad70eb121d6b88a9afbee36e/VPhoneExecutable/VPhoneCommand/VPhoneRestore/MobileRecoveryCore/COPYING) is retained in [LICENSES/libirecovery-LGPL-2.1.txt](LICENSES/libirecovery-LGPL-2.1.txt). The separately fetched libirecovery revision in the lock carries the same source-header notice and license text.

## SMC benchmark fixture

The derived fixture files [main.c](benchmarks/smc/main.c), [start.S](benchmarks/smc/start.S), and [link.ld](benchmarks/smc/link.ld) carry `SPDX-License-Identifier: GPL-2.0-or-later` and identify their [vphone-fence source at 38c32a9bf16d92c0fd1d82b20c051cbe34a90372](https://github.com/YahiaAngelo/qemu-vphone/tree/38c32a9bf16d92c0fd1d82b20c051cbe34a90372/tests/tcg/vphone-fence). The original fixture's `main.c` carries that SPDX identifier; its boot and linker files have no separate license header and are covered by the no-header GPL version 2-or-later statement in QEMU's notice.

The complete GPL version 2 text is already retained in [benchmarks/smc/COPYING](benchmarks/smc/COPYING). The [benchmark README](benchmarks/smc/README.md) also credits the fixture and epoch implementation with exact commits. The benchmark runner is original Limulator code under the root MIT license.

## Generic-timer regression fixture

The derived C, assembly, and linker sources in [benchmarks/timer](benchmarks/timer/README.md) retain `GPL-2.0-or-later` notices and credit [vphone-cnt at 108e1421d208e2f17d6a8d5b98ac7659c3517873](https://github.com/YahiaAngelo/qemu-vphone/tree/108e1421d208e2f17d6a8d5b98ac7659c3517873/tests/tcg/vphone-cnt), introduced by [dc666bc8dd40bd6e2285f544928415b518bb29d7](https://github.com/YahiaAngelo/qemu-vphone/commit/dc666bc8dd40bd6e2285f544928415b518bb29d7). The complete GPL version 2 text is retained in [benchmarks/smc/COPYING](benchmarks/smc/COPYING). The Python runner and shell scope wrapper are original Limulator code under the root MIT license.
