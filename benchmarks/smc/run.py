#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Build and measure the diskless AArch64 self-modifying-code fixture."""

import argparse
import hashlib
import json
import math
import mmap
import os
import platform
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

BLOCKS, ROUNDS, VCPUS = 512, 2, 9
HERE = Path(__file__).resolve().parent


class BenchmarkError(Exception):
    pass


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def now():
    return datetime.now(UTC).isoformat()


def bounded_repetitions(value):
    number = int(value)
    if not 1 <= number <= 5:
        raise argparse.ArgumentTypeError("repetitions must be between 1 and 5")
    return number


def bounded_timeout(value):
    seconds = float(value)
    if not math.isfinite(seconds) or not 1 <= seconds <= 300:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 300 seconds")
    return seconds


def executable(value, path):
    found = shutil.which(str(Path(value).expanduser()), path=path)
    if found is None:
        raise BenchmarkError(f"executable not found: {value}")
    # LLD chooses its driver from argv[0], so retain names such as ld.lld.
    return Path(found).absolute()


def run_command(report, output, label, args, env, timeout):
    record = {
        "label": label,
        "args": [str(arg) for arg in args],
        "cwd": str(output),
        "timeout_seconds": timeout,
        "environment": {
            key: value for key, value in env.items() if key.startswith("VPHONE_") or key == "LD_LIBRARY_PATH"
        },
        "stdout": f"{label}.stdout.txt",
        "stderr": f"{label}.stderr.txt",
    }
    report["commands"].append(record)
    start = time.perf_counter()
    process = subprocess.Popen(
        record["args"],
        cwd=output,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    timed_out = False
    interrupted = None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    except BaseException as error:
        interrupted = error
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    record.update(
        returncode=process.returncode, timed_out=timed_out, wall_seconds=time.perf_counter() - start
    )
    for key, content in (("stdout", stdout), ("stderr", stderr)):
        with (output / record[key]).open("x") as stream:
            stream.write(content)
    if interrupted is not None:
        raise interrupted
    if timed_out or process.returncode:
        reason = f"exceeded {timeout:g}s" if timed_out else f"exited {process.returncode}"
        raise BenchmarkError(f"{label} {reason}; see {output / record['stderr']}")
    return record, stdout.replace("\r", "")


def parse_sample(stdout):
    def values(key):
        return [int(value) for value in re.findall(rf"^{key}=(\d+)$", stdout, re.MULTILINE)]

    expected = {
        "blocks": BLOCKS,
        "bytes": BLOCKS * 64,
        "errors": 0,
        "stale_code_negative_control_mismatches": BLOCKS,
        "verified_function_executions": (1 + ROUNDS) * BLOCKS * 2 + BLOCKS * 2,
        "verified_modified_instruction_executions": ROUNDS * BLOCKS * 2 * 15,
    }
    for key, value in expected.items():
        if values(key) != [value]:
            raise BenchmarkError(f"fixture correctness/control failed: {key}={values(key)}, expected {value}")
    if stdout.splitlines().count("TOTAL ok") != 1 or re.search(
        r"^(FAIL|FATAL|TOTAL fail)", stdout, re.MULTILINE
    ):
        raise BenchmarkError("fixture did not finish with TOTAL ok")
    if values("phase") != list(range(1, ROUNDS + 1)) or values("cumulative_errors") != [0] * ROUNDS:
        raise BenchmarkError("fixture did not verify both rewrite phases")
    frequency = values("counter_hz")
    hot, cold = values("hot_copy_ticks"), values("cold_copy_ticks")
    if len(frequency) != 1 or frequency[0] <= 0 or len(hot) != ROUNDS or len(cold) != ROUNDS:
        raise BenchmarkError("fixture timing fields are incomplete")
    if any(not 0 < c < h for h, c in zip(hot, cold, strict=True)):
        raise BenchmarkError("cold-copy sensitivity control failed; repeat on an idle host")
    return {
        "correctness_ok": True,
        "sensitivity_ok": True,
        "counter_hz": frequency[0],
        "hot_copy_ticks": hot,
        "cold_copy_ticks": cold,
        "hot_copy_seconds": [value / frequency[0] for value in hot],
        "cold_copy_seconds": [value / frequency[0] for value in cold],
        **expected,
    }


def summarize(samples):
    modes = {}
    for epoch in (0, 1):
        group = [sample for sample in samples if sample["epoch"] == epoch]
        modes[str(epoch)] = {
            "runs": len(group),
            "hot_phases": len(group) * ROUNDS,
            "median_hot_copy_seconds": statistics.median(
                value for sample in group for value in sample["hot_copy_seconds"]
            ),
            "median_cold_copy_seconds": statistics.median(
                value for sample in group for value in sample["cold_copy_seconds"]
            ),
            "median_fixture_wall_seconds": statistics.median(sample["wall_seconds"] for sample in group),
        }
    return {
        "modes": modes,
        "full_clear_over_epoch_hot_copy_ratio": (
            modes["0"]["median_hot_copy_seconds"] / modes["1"]["median_hot_copy_seconds"]
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", help="actual QEMU executable with Limulator's epoch invalidation patch")
    parser.add_argument(
        "--bios", type=Path, help="QEMU pc-bios directory; detected from the backend by default"
    )
    parser.add_argument("--clang", default="clang", help="AArch64-capable Clang executable")
    parser.add_argument("--linker", default="ld.lld", help="LLD executable")
    parser.add_argument(
        "--repetitions", type=bounded_repetitions, default=3, help="interleaved 0/1 pairs, 1–5 (default 3)"
    )
    parser.add_argument(
        "--timeout", type=bounded_timeout, default=60.0, help="deadline per QEMU run, 1–300 seconds"
    )
    parser.add_argument("--output", type=Path, help="new output directory; existing paths are rejected")
    args = parser.parse_args()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = (
        (args.output or Path("artifacts") / f"smc-{stamp}-{uuid.uuid4().hex[:8]}").expanduser().absolute()
    )
    if output.exists():
        parser.error(f"output path already exists: {output}")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    home = Path(os.environ.get("LIMULATOR_HOME") or data_home / "limulator").expanduser().resolve()
    backend = (
        Path(os.environ.get("LIMULATOR_BACKEND") or home / "backends/vphone-linux").expanduser().resolve()
    )
    prefix = Path(os.environ.get("LIMULATOR_PREFIX") or home / "tools").expanduser().resolve()
    env = {key: value for key, value in os.environ.items() if not key.startswith("VPHONE_")}
    env["PATH"] = str(prefix / "bin") + os.pathsep + env.get("PATH", os.defpath)
    libraries = [str(prefix / name) for name in ("lib", "lib64") if (prefix / name).is_dir()]
    if env.get("LD_LIBRARY_PATH"):
        libraries.append(env["LD_LIBRARY_PATH"])
    if libraries:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(libraries)
    env.update(VPHONE_FENCE="1", VPHONE_INLINE_LOOKUP="2")
    try:
        qemu = executable(args.qemu or str(backend / "qemu/build/qemu-system-aarch64"), env["PATH"])
        clang, linker = (executable(value, env["PATH"]) for value in (args.clang, args.linker))
        with qemu.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as binary:
            if binary.find(b"VPHONE_JC_LAZY_INVAL") == -1:
                raise BenchmarkError("QEMU lacks VPHONE_JC_LAZY_INVAL; build the backend with patch 0008")
        bios = (
            args.bios.expanduser().resolve()
            if args.bios
            else next(
                (
                    path
                    for path in (
                        qemu.parent / "pc-bios",
                        qemu.parent.parent / "pc-bios",
                        backend / "qemu/pc-bios",
                    )
                    if (path / "efi-virtio.rom").is_file()
                ),
                None,
            )
        )
        if args.bios and not bios.is_dir():
            raise BenchmarkError(f"BIOS directory does not exist: {bios}")
    except (BenchmarkError, OSError, ValueError) as error:
        parser.error(str(error))
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        parser.error(f"cannot create exclusive output directory: {error}")
    report = {
        "schema_version": 1,
        "status": "running",
        "started_at": now(),
        "invocation": [sys.executable, str(HERE / "run.py"), *sys.argv[1:]],
        "scope": "Diskless AArch64 virt self-modifying-code microbenchmark; not iOS app performance or FPS.",
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "load_average_start": os.getloadavg(),
        },
        "configuration": {
            "blocks": BLOCKS,
            "rewrite_phases": ROUNDS,
            "allocated_vcpus": VCPUS,
            "executing_vcpus": 2,
            "memory_mib": 256,
            "repetitions": args.repetitions,
        },
        "paths": {
            "home": str(home),
            "backend": str(backend),
            "prefix": str(prefix),
            "qemu": str(qemu),
            "bios": str(bios) if bios else None,
        },
        "sha256": {},
        "versions": {"python": sys.version},
        "commands": [],
        "samples": [],
    }
    exit_code = 1
    try:
        sources = {name: sha256(HERE / name) for name in ("main.c", "start.S", "link.ld", "run.py")}
        report["sha256"] = {
            "sources": sources,
            "qemu": sha256(qemu),
            "clang": sha256(clang),
            "linker": sha256(linker),
        }
        for label, tool in (("qemu", qemu), ("clang", clang), ("linker", linker)):
            _, version = run_command(report, output, f"version-{label}", [tool, "--version"], env, 10)
            report["versions"][label] = version.strip()
        flags = [
            "--target=aarch64-none-elf",
            "-march=armv8.5-a",
            "-mstrict-align",
            "-ffreestanding",
            "-fno-builtin",
            "-nostdlib",
            "-O2",
            "-Wall",
            "-Wextra",
        ]
        for name in ("start", "main"):
            source = HERE / (name + (".S" if name == "start" else ".c"))
            run_command(
                report,
                output,
                f"compile-{name}",
                [clang, *flags, "-c", source, "-o", output / f"{name}.o"],
                env,
                60,
            )
        elf = output / "smc-copy.elf"
        run_command(
            report,
            output,
            "link",
            [linker, "-T", HERE / "link.ld", output / "start.o", output / "main.o", "-o", elf],
            env,
            60,
        )
        report["sha256"]["fixture_elf"] = sha256(elf)
        common = (["-L", str(bios)] if bios else []) + [
            "-M",
            "virt",
            "-accel",
            "tcg,thread=multi",
            "-cpu",
            "neoverse-v1",
            "-smp",
            str(VCPUS),
            "-m",
            "256M",
            "-nographic",
            "-no-reboot",
            "-kernel",
            str(elf),
        ]
        for repetition in range(1, args.repetitions + 1):
            for epoch in (0, 1):
                child_env = dict(env, VPHONE_JC_LAZY_INVAL=str(epoch))
                label = f"run-{repetition:02d}-epoch{epoch}"
                command, stdout = run_command(report, output, label, [qemu, *common], child_env, args.timeout)
                sample = {
                    "repetition": repetition,
                    "epoch": epoch,
                    "command": label,
                    "wall_seconds": command["wall_seconds"],
                    **parse_sample(stdout),
                }
                report["samples"].append(sample)
                print(f"{label}: correctness and controls passed", flush=True)
        if sha256(qemu) != report["sha256"]["qemu"] or any(
            sha256(HERE / name) != digest for name, digest in sources.items()
        ):
            raise BenchmarkError("QEMU or fixture sources changed during the benchmark")
        report["summary"] = summarize(report["samples"])
        report["status"] = "passed"
        medians = report["summary"]["modes"]
        print(
            "Median hot-copy phase: "
            + "; ".join(f"epoch{mode} {medians[mode]['median_hot_copy_seconds']:.6f}s" for mode in ("0", "1"))
        )
        exit_code = 0
    except KeyboardInterrupt:
        report.update(status="interrupted", error="interrupted by user")
        exit_code = 130
    except (BenchmarkError, OSError) as error:
        report.update(status="failed", error=str(error))
        print(f"error: {error}", file=sys.stderr)
    finally:
        report["completed_at"] = now()
        report["host"]["load_average_end"] = os.getloadavg()
        with (output / "results.json").open("x") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        print(f"Results: {output / 'results.json'}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
