# Generic timer regression

This diskless AArch64 fixture reproduces the 24 MHz counter-rate and WFIT deadline
bugs fixed by [QEMU commit 3e884e8](https://github.com/YahiaAngelo/qemu-vphone/commit/3e884e825cd6d545f9774c236113cfd8745e204e),
retained in Limulator's patch 0014. It checks timer correctness. It does not run
iOS or establish a cause of migration, app-launch, or graphics failures.

Use two separately built QEMU binaries with identical build settings. The validated
baseline was [14c9913](https://github.com/YahiaAngelo/qemu-vphone/tree/14c9913d2231080bd234c69421ed4f293a25f767)
plus Limulator patches 0006 and 0008. The candidate added only patch 0014. Both
need the `aarch64-softmmu` target, TCG, the `virt` machine, and the `max` CPU's
`cntfrq` property and WFxT feature. PVG/Reims is unnecessary. This runner does not
build or replace either QEMU binary.

Requirements: Linux with a working systemd user manager and cgroup v2 memory/CPU
controllers, `uv` with Python 3.12 or later, AArch64-capable Clang, LLD, and
`readelf`. Clang/LLD 18.1.3 were used for validation. The conservative preflight
requires at least 32 GiB `MemAvailable` and 4 GiB `SwapFree`, matching the original
diagnostic run alongside native creation. Each actual fixture uses 64 MiB guest
RAM and has a 1 GiB cgroup memory cap with no swap.

From the repository root:

```sh
uv run benchmarks/timer/run.py \
  --baseline /absolute/path/to/baseline/qemu-system-aarch64 \
  --candidate /absolute/path/to/candidate/qemu-system-aarch64 \
  --output /tmp/timer-regression
```

The output directory must not exist. `--clang`, `--linker`, and `--readelf` select
tools explicitly. Required shared-library paths may be supplied through
`LD_LIBRARY_PATH`; the runner preserves and records that value. It clears
inherited `VPHONE_*` overrides. It writes compiled ELFs, commands, hashes,
readelf output, serial/host logs, and `results.json` under the new directory.

Eight runs execute sequentially, each with one vCPU, a 32 MiB TCG cache, low
priority, a 100% CPU quota, and a 20-second child deadline. They use no disk
image, USB, VSOCK, network, GPU, monitor, GDB, or listening port. Timeouts and
interrupts kill only that run's owned scope and process group. Coordinate with
the native VM owner before running diagnostics alongside creation.

The rate test uses `-icount shift=0,sleep=off`: two million known instructions
advance virtual time by two million nanoseconds. The expected counter change is
48,000 ticks at 24 MHz, with four ticks of instruction-boundary tolerance. The
old integer period gives 48,780. A 25 MHz control divides a nanosecond clock
exactly and must pass on both binaries.

The separate WFIT test runs without icount, masks interrupts, disables generic
timers, and checks eight 10 ms future deadlines after initial uptime. The old
24 MHz implementation interprets absolute ticks as nanoseconds and returns
early; the candidate must reach every target. At 1 GHz, ticks and nanoseconds
have the same units, so both controls must pass. Every run reports actual
`CNTFRQ_EL0`, `ID_AA64ISAR2_EL1.WFXT`, and exception level. Unsupported properties,
wrong feature values, incomplete output, missed negative controls, or changed
binaries/sources fail the runner. Heavy host preemption can hide an early-return
negative control; that is an inconclusive failure, not a passing regression.

The isolated validation produced:

| Check | Baseline | Candidate |
|---|---:|---:|
| 24 MHz rate, expected 48,000 | 48,780 | 48,000 |
| 25 MHz rate, expected 50,000 | 50,000 | 50,000 |
| 24 MHz WFIT early returns | 8/8 | 0/8 |
| 1 GHz WFIT early returns | 0/8 | 0/8 |

Boot, exception, UART, and linker support derive from
[vphone-cnt at commit 108e1421](https://github.com/YahiaAngelo/qemu-vphone/tree/108e1421d208e2f17d6a8d5b98ac7659c3517873/tests/tcg/vphone-cnt).
The C, assembly, and linker fixture files carry GPL-2.0-or-later notices. The
complete GPL version 2 text is retained in [../smc/COPYING](../smc/COPYING).
The Python runner and shell scope wrapper are original Limulator code under
the root MIT license.
