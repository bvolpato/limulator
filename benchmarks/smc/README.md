# Self-modifying-code microbenchmark

This diskless AArch64 `virt` fixture measures copying new instructions over
already translated code. It isolates QEMU's PC-relative translation-block
invalidation cost. It does not run iOS or measure app launch time, graphics, or FPS.

Requirements: Linux, `uv` with Python 3.12 or later, AArch64-capable Clang,
`ld.lld`, and the vphone QEMU backend built with Limulator's patch 0008.
Clang/LLD 18.1.3 were used for the original fixture. The runner rejects a QEMU
binary without the `VPHONE_JC_LAZY_INVAL` switch.

From the repository root:

```sh
uv run benchmarks/smc/run.py --output artifacts/smc-measurement
```

The output directory must not exist. The default is three interleaved pairs
(`0, 1, 0, 1, 0, 1`). Use `--repetitions 1` for a smoke check or up to five
pairs for a measurement. Each QEMU invocation has a 60-second deadline;
`--timeout` accepts 1–300 seconds. Interrupting or timing out the runner kills
its child process group.

The runner uses `LIMULATOR_HOME`, `LIMULATOR_BACKEND`, and `LIMULATOR_PREFIX`
with the same defaults as `tools/backend_env.sh`. It prepends private prefix
libraries to `LD_LIBRARY_PATH`. Alternate tools are explicit:

```sh
uv run benchmarks/smc/run.py \
  --qemu /path/to/qemu-system-aarch64 --bios /path/to/pc-bios \
  --clang clang-18 --linker ld.lld-18 --repetitions 1 \
  --output /tmp/smc-smoke
```

The tracked C, assembly, and linker inputs are compiled into the new output
directory. No firmware or VM disk is required. QEMU allocates 256 MiB of guest
RAM and nine vCPU jump caches; two vCPUs execute the fixture. Both epoch modes
use the same executable, `VPHONE_FENCE=1`, and `VPHONE_INLINE_LOOKUP=2`.
Other inherited `VPHONE_*` settings are cleared for the child commands.

The fixture primes 512 distinct 64-byte functions on both CPUs, then rewrites
32 KiB twice. Each function has one immediate load, fourteen immediate adds,
and a return. All fifteen arithmetic instructions change monotonically, so
any stale arithmetic instruction produces the wrong sum. Every copied byte
is checked, and both CPUs execute every rewritten function. Before rewriting,
the stale-code negative control must reject all 512 old functions. Copying
the same bytes into an untranslated region is the cold-copy timing control.
The runner fails if correctness counts, either rewrite phase, or the controls
fail. Cold copies must take positive time and finish faster than hot copies.

`results.json` records the schema version, host/load context, exact child
argument arrays and environment overrides, tool versions, source/tool/QEMU/ELF
SHA-256 hashes, raw samples, and medians. Each command also has stdout/stderr
logs. Failed runs retain their diagnostics and return a nonzero exit status.
QEMU and source hashes are checked again before accepting a result.

Compare `summary.modes.0.median_hot_copy_seconds` with mode `1`. Mode `0`
clears every jump cache for each invalidated PC-relative block; mode `1`
marks a new invalidation epoch. The median pools two hot-copy phases per run.
Whole-fixture wall time includes startup, compilation of guest blocks, and
verification, so keep it separate from the copy-phase measurement.

Run measurements on an otherwise idle host. A concurrent restore, compiler,
or VM can distort the result; the load snapshots are context, not proof of
isolation. A single smoke pair establishes functionality, not a publishable
performance result. The previously recorded three-pair result was 0.6417 s
versus 0.000630 s per hot-copy phase with nine allocated caches. Those are
microbenchmark numbers, not a whole-app speedup.

Boot, exception, UART, and linker support derive from
[vphone-fence at commit 38c32a9](https://github.com/YahiaAngelo/qemu-vphone/tree/38c32a9bf16d92c0fd1d82b20c051cbe34a90372/tests/tcg/vphone-fence).
The epoch implementation is
[upstream commit f6b3bc9](https://github.com/YahiaAngelo/qemu-vphone/commit/f6b3bc9d8ec9ba3113a4dbae9e9584775cd0d1f2).
The fixture sources retain GPL-2.0-or-later SPDX notices; the GPL text is in
[COPYING](COPYING).
