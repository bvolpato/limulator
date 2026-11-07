# Third-party notices

The [MIT license](LICENSE) covers Limulator's original code. The derived patch excerpts and fixture sources below retain their upstream notices and licenses. The copies in `LICENSES/` reproduce the pinned upstream texts without changing those terms.

Repositories, revisions, patch targets, and patch hashes are recorded in [third_party.lock.json](third_party.lock.json). The notices here cover the included derived patches and SMC fixture; the lock also identifies dependencies fetched separately by the build.

## vphone-linux

The backend launcher, tools, and Linux build/edit rules modified by patches 0001–0005, 0007, 0009, 0010, 0012, and 0013 come from [vphone-linux at c2dc1996c8398ddb994288a29a719360f354d268](https://github.com/YahiaAngelo/vphone-linux/tree/c2dc1996c8398ddb994288a29a719360f354d268).

Its notice is `Copyright (c) 2026 Yahia`, under the MIT License. The complete notice, permission text, disclaimer, and repository-scope statement are retained in [LICENSES/vphone-linux-MIT.txt](LICENSES/vphone-linux-MIT.txt), copied from the [pinned LICENSE](https://github.com/YahiaAngelo/vphone-linux/blob/c2dc1996c8398ddb994288a29a719360f354d268/LICENSE).

Patch 0005 also modifies bundled libirecovery code through an edit rule; that source's separate notice is recorded below. Patch 0009's edit rule changes vphone-cli's guest-device definition, whose project notice is also recorded below.

## vphone-cli

Patch 0011 modifies the dyld-cache chunk reader and X1 planner from [vphone-cli at 83197152137ddd2aad70eb121d6b88a9afbee36e](https://github.com/Lakr233/vphone-cli/tree/83197152137ddd2aad70eb121d6b88a9afbee36e):

- [DyldSharedCacheChunkSet.swift](https://github.com/Lakr233/vphone-cli/blob/83197152137ddd2aad70eb121d6b88a9afbee36e/VPhoneExecutable/VPhoneCommand/FirmwarePatcher/DyldSharedCache/DyldSharedCacheChunkSet.swift)
- [DyldSharedCacheArm64eX1BootstrapPatcher.swift](https://github.com/Lakr233/vphone-cli/blob/83197152137ddd2aad70eb121d6b88a9afbee36e/VPhoneExecutable/VPhoneCommand/FirmwarePatcher/DyldSharedCache/Patchers/DyldSharedCacheArm64eX1BootstrapPatcher.swift)

The project notice is `Copyright (c) 2026 @Lakr233`, under the MIT License. The complete text is retained in [LICENSES/vphone-cli-MIT.txt](LICENSES/vphone-cli-MIT.txt), copied from the [pinned LICENSE](https://github.com/Lakr233/vphone-cli/blob/83197152137ddd2aad70eb121d6b88a9afbee36e/LICENSE).

## QEMU patches

The QEMU base is [qemu-vphone at 14c9913d2231080bd234c69421ed4f293a25f767](https://github.com/YahiaAngelo/qemu-vphone/tree/14c9913d2231080bd234c69421ed4f293a25f767). Patch 0006 ports acquire/release ordering changes from [38c32a9bf16d92c0fd1d82b20c051cbe34a90372](https://github.com/YahiaAngelo/qemu-vphone/commit/38c32a9bf16d92c0fd1d82b20c051cbe34a90372). Patch 0008 ports jump-cache invalidation changes from [f6b3bc9d8ec9ba3113a4dbae9e9584775cd0d1f2](https://github.com/YahiaAngelo/qemu-vphone/commit/f6b3bc9d8ec9ba3113a4dbae9e9584775cd0d1f2). Both commits record YahiaAngelo as author, and their identifiers remain in the patches and lock.

The modified source files carry these notices:

| Patch | Pinned source file | Copyright notice | File license |
|---|---|---|---|
| 0006 | [target/arm/tcg/translate-a64.c](https://github.com/YahiaAngelo/qemu-vphone/blob/14c9913d2231080bd234c69421ed4f293a25f767/target/arm/tcg/translate-a64.c) | `Copyright (c) 2013 Alexander Graf <agraf@suse.de>` | LGPL version 2.1 or later |
| 0008 | [accel/tcg/cpu-exec.c](https://github.com/YahiaAngelo/qemu-vphone/blob/14c9913d2231080bd234c69421ed4f293a25f767/accel/tcg/cpu-exec.c) | `Copyright (c) 2003-2005 Fabrice Bellard` | LGPL version 2.1 or later |
| 0008 | [accel/tcg/tb-maint.c](https://github.com/YahiaAngelo/qemu-vphone/blob/14c9913d2231080bd234c69421ed4f293a25f767/accel/tcg/tb-maint.c) | `Copyright (c) 2003 Fabrice Bellard` | LGPL version 2.1 or later |
| 0008 | [accel/tcg/tb-jmp-cache.h](https://github.com/YahiaAngelo/qemu-vphone/blob/14c9913d2231080bd234c69421ed4f293a25f767/accel/tcg/tb-jmp-cache.h) | `Copyright (c) 2003 Fabrice Bellard` | `GPL-2.0-or-later` |

The patches leave those source headers unchanged. The complete pinned license texts are copied from [COPYING](https://github.com/YahiaAngelo/qemu-vphone/blob/14c9913d2231080bd234c69421ed4f293a25f767/COPYING) to [LICENSES/qemu-GPL-2.0.txt](LICENSES/qemu-GPL-2.0.txt) and from [COPYING.LIB](https://github.com/YahiaAngelo/qemu-vphone/blob/14c9913d2231080bd234c69421ed4f293a25f767/COPYING.LIB) to [LICENSES/qemu-LGPL-2.1.txt](LICENSES/qemu-LGPL-2.1.txt).

The fork's [pinned LICENSE](https://github.com/YahiaAngelo/qemu-vphone/blob/14c9913d2231080bd234c69421ed4f293a25f767/LICENSE) also records the ChefKissInc/QEMUAppleSilicon provenance, its project-level GPL version 3 and own-parts AGPL version 3 statements, and the original QEMU license notice. That full notice is retained in [LICENSES/qemu-NOTICE.txt](LICENSES/qemu-NOTICE.txt). Its accompanying [COPYING_GPL-3.0](https://github.com/YahiaAngelo/qemu-vphone/blob/14c9913d2231080bd234c69421ed4f293a25f767/COPYING_GPL-3.0) and [COPYING_AGPL-3.0](https://github.com/YahiaAngelo/qemu-vphone/blob/14c9913d2231080bd234c69421ed4f293a25f767/COPYING_AGPL-3.0) texts are retained as [qemu-GPL-3.0.txt](LICENSES/qemu-GPL-3.0.txt) and [qemu-AGPL-3.0.txt](LICENSES/qemu-AGPL-3.0.txt). The file-specific notices above remain distinct.

## libirecovery

Patch 0005's edit rule changes [MobileRecoveryCore/libirecovery.c bundled in the pinned vphone-cli source](https://github.com/Lakr233/vphone-cli/blob/83197152137ddd2aad70eb121d6b88a9afbee36e/VPhoneExecutable/VPhoneCommand/VPhoneRestore/MobileRecoveryCore/libirecovery.c). Its retained header lists:

- `Copyright (c) 2011-2023 Nikias Bassen <nikias@gmx.li>`
- `Copyright (c) 2012-2020 Martin Szulecki <martin.szulecki@libimobiledevice.org>`
- `Copyright (c) 2010 Chronic-Dev Team`
- `Copyright (c) 2010 Joshua Hill`
- `Copyright (c) 2008-2011 Nicolas Haunold`

The header states GNU Lesser General Public License version 2.1 and retains its warranty disclaimer. The complete bundled [COPYING](https://github.com/Lakr233/vphone-cli/blob/83197152137ddd2aad70eb121d6b88a9afbee36e/VPhoneExecutable/VPhoneCommand/VPhoneRestore/MobileRecoveryCore/COPYING) is copied to [LICENSES/libirecovery-LGPL-2.1.txt](LICENSES/libirecovery-LGPL-2.1.txt). The separately fetched [libirecovery revision b59ef4814525f487287da1609864f432cd79e3ed](https://github.com/libimobiledevice/libirecovery/tree/b59ef4814525f487287da1609864f432cd79e3ed), recorded under `apple_usb` in the lock, carries the same source-header notice and license text.

## SMC benchmark fixture

The derived fixture files [main.c](benchmarks/smc/main.c), [start.S](benchmarks/smc/start.S), and [link.ld](benchmarks/smc/link.ld) carry `SPDX-License-Identifier: GPL-2.0-or-later` and identify their [vphone-fence source at 38c32a9bf16d92c0fd1d82b20c051cbe34a90372](https://github.com/YahiaAngelo/qemu-vphone/tree/38c32a9bf16d92c0fd1d82b20c051cbe34a90372/tests/tcg/vphone-fence). The original fixture's `main.c` carries that SPDX identifier; its boot and linker files have no separate license header and are covered by the no-header GPL version 2-or-later statement in QEMU's notice.

The complete GPL version 2 text is already retained in [benchmarks/smc/COPYING](benchmarks/smc/COPYING). The [benchmark README](benchmarks/smc/README.md) also credits the fixture and epoch implementation with exact commits. The benchmark runner is original Limulator code under the root MIT license.
