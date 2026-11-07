#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Compare explicit baseline/candidate QEMU binaries with diskless AArch64 timer regressions."""

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES = ("main.c", "measure.S", "start.S", "link.ld", "enter-fixture.sh", "run.py")


class FixtureError(Exception):
    pass


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def executable(name):
    found = shutil.which(str(Path(name).expanduser()))
    if found is None:
        raise FixtureError(f"executable not found: {name}")
    # Preserve ld.lld's driver name even when it is a symlink.
    return Path(found).absolute()


def preflight():
    available = {
        line.split(":")[0]: int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("MemAvailable:", "SwapFree:"))
    }
    if available["MemAvailable"] < 32 * 1024 * 1024 or available["SwapFree"] < 4 * 1024 * 1024:
        raise FixtureError(f"requires 32 GiB MemAvailable and 4 GiB SwapFree: {available}")
    return available


def run_command(report, output, label, arguments, environment, timeout=60, scope=None):
    record = {"label": label, "args": [str(arg) for arg in arguments], "timeout_seconds": timeout}
    report["commands"].append(record)
    started = time.monotonic()
    with (output / f"{label}.host.log").open("x") as log:
        process = subprocess.Popen(
            record["args"],
            cwd=output,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            process.wait(timeout=timeout)
        except BaseException:
            if scope:
                subprocess.run(
                    ["systemctl", "--user", "kill", "--kill-whom=all", "--signal=KILL", scope],
                    check=False,
                    capture_output=True,
                )
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        finally:
            record.update(returncode=process.returncode, wall_seconds=time.monotonic() - started)
    if process.returncode:
        raise FixtureError(f"{label} exited {process.returncode}; see {output / (label + '.host.log')}")
    return record


def validate_sample(serial, mode, case, frequency):
    values = {}
    lines = serial.replace("\r", "").splitlines()
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            if value.isdecimal():
                values.setdefault(key, []).append(int(value))
    negative = mode == "baseline" and frequency == 24_000_000
    expected = "TOTAL fail" if negative else "TOTAL ok"
    valid = (
        values.get("counter_hz") == [frequency]
        and len(values.get("wfxt", [])) == 1
        and values["wfxt"][0] >= 2
        and values.get("el") == [1]
        and values.get("failures") == [1 if negative else 0]
        and lines.count(expected) == 1
        and not any(line.startswith("FATAL") for line in lines)
        and ("TOTAL ok" if negative else "TOTAL fail") not in lines
        and lines.count(f"case={case}") == 1
    )
    if case == "rate":
        expected_ticks = 2_000_000 * frequency // 1_000_000_000
        valid = (
            valid
            and values.get("loop_instructions") == [2_000_000]
            and values.get("expected_ticks") == [expected_ticks]
            and values.get("tolerance_ticks") == [4]
            and len(values.get("ticks", [])) == 1
        )
        if valid:
            difference = abs(values["ticks"][0] - expected_ticks)
            valid = difference > 4 if negative else difference <= 4
    else:
        valid = (
            valid
            and values.get("trial") == list(range(8))
            and len(values.get("early_returns", [])) == 1
            and all(len(values.get(key, [])) == 8 for key in ("before", "deadline", "after"))
        )
        if valid:
            early = sum(
                after < target for after, target in zip(values["after"], values["deadline"], strict=True)
            )
            valid = (
                early == values["early_returns"][0]
                and (early > 0 if negative else early == 0)
                and all(
                    target - before == frequency // 100
                    for target, before in zip(values["deadline"], values["before"], strict=True)
                )
            )
    if not valid:
        raise FixtureError(f"inconclusive or failing fixture: {mode}-{case}-{frequency}: {values}")
    return {"values": values, "expected_negative_control": negative, "valid": True}


def run_case(report, output, qemu, elf, mode, case, frequency, token, environment):
    available = preflight()
    name = f"{mode}-{case}-{frequency}"
    serial_path = output / f"{name}.serial.log"
    command = [
        qemu,
        "-machine",
        "virt,secure=off,virtualization=off",
        "-accel",
        "tcg,thread=single,tb-size=32",
        "-cpu",
        f"max,cntfrq={frequency}",
        "-smp",
        "1",
        "-m",
        "64M",
        "-nodefaults",
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        f"file:{serial_path}",
        "-nic",
        "none",
        "-no-reboot",
        "-kernel",
        elf,
    ]
    if case == "rate":
        command += ["-icount", "shift=0,sleep=off"]
    unit = f"limulator-timer-{token}-{name}.scope"
    wrapped = [
        "systemd-run",
        "--quiet",
        "--user",
        "--scope",
        f"--unit={unit}",
        "--property=MemoryHigh=512M",
        "--property=MemoryMax=1G",
        "--property=MemorySwapMax=0",
        "--property=CPUWeight=10",
        "--property=CPUQuota=100%",
        "--property=IOWeight=10",
        "--property=TasksMax=64",
        "bash",
        HERE / "enter-fixture.sh",
        *command,
    ]
    record = run_command(report, output, name, wrapped, environment, timeout=25, scope=unit)
    log = (output / f"{name}.host.log").read_text()
    for limit in (
        "memory.high=536870912",
        "memory.max=1073741824",
        "memory.swap.max=0",
        "cpu.max=100000 100000",
    ):
        if limit not in log.splitlines():
            raise FixtureError(f"unverified cgroup limit: {name}: {limit}")
    sample = validate_sample(serial_path.read_text(), mode, case, frequency)
    report["cases"].append(
        {
            "name": name,
            "command": [str(arg) for arg in command],
            "preflight_kib": available,
            "wall_seconds": record["wall_seconds"],
            **sample,
        }
    )
    print(f"{name}: expected result verified", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="QEMU without timer fix 3e884e8")
    parser.add_argument("--candidate", required=True, help="same QEMU source/configuration with timer fix")
    parser.add_argument("--output", required=True, type=Path, help="new output directory")
    parser.add_argument("--clang", default="clang-18")
    parser.add_argument("--linker", default="ld.lld-18")
    parser.add_argument("--readelf", default="readelf")
    args = parser.parse_args()
    output = args.output.expanduser().absolute()
    try:
        binaries = {mode: executable(getattr(args, mode)) for mode in ("baseline", "candidate")}
        tools = {name: executable(getattr(args, name)) for name in ("clang", "linker", "readelf")}
        preflight()
        output.mkdir(parents=True, exist_ok=False)
    except (FixtureError, OSError) as error:
        parser.error(str(error))
    environment = {key: value for key, value in os.environ.items() if not key.startswith("VPHONE_")}
    report = {
        "schema_version": 1,
        "status": "running",
        "commands": [],
        "cases": [],
        "scope": "Diskless timer correctness only; no iOS startup or performance conclusion.",
        "sha256": {
            "sources": {name: sha256(HERE / name) for name in SOURCES},
            "binaries": {mode: sha256(path) for mode, path in binaries.items()},
            "tools": {name: sha256(path) for name, path in tools.items()},
        },
        "library_path": environment.get("LD_LIBRARY_PATH", ""),
    }
    exit_code = 1
    try:
        flags = [
            "--target=aarch64-none-elf",
            "-march=armv8.7-a",
            "-mgeneral-regs-only",
            "-mstrict-align",
            "-ffreestanding",
            "-fno-builtin",
            "-fno-stack-protector",
            "-nostdlib",
            "-O2",
            "-Wall",
            "-Wextra",
        ]
        for name in ("start", "measure"):
            run_command(
                report,
                output,
                f"compile-{name}",
                [tools["clang"], *flags, "-c", HERE / f"{name}.S", "-o", output / f"{name}.o"],
                environment,
            )
        elfs = {}
        for number, case in ((1, "rate"), (2, "wfit")):
            run_command(
                report,
                output,
                f"compile-{case}",
                [
                    tools["clang"],
                    *flags,
                    f"-DTEST_MODE={number}",
                    "-c",
                    HERE / "main.c",
                    "-o",
                    output / f"main{number}.o",
                ],
                environment,
            )
            elfs[case] = output / f"timer{number}.elf"
            run_command(
                report,
                output,
                f"link-{case}",
                [
                    tools["linker"],
                    "-T",
                    HERE / "link.ld",
                    output / "start.o",
                    output / "measure.o",
                    output / f"main{number}.o",
                    "-o",
                    elfs[case],
                ],
                environment,
            )
            run_command(report, output, f"readelf-{case}", [tools["readelf"], "-h", elfs[case]], environment)
        report["sha256"]["elfs"] = {case: sha256(path) for case, path in elfs.items()}
        token = uuid.uuid4().hex[:8]
        for case, frequency in (
            ("rate", 24_000_000),
            ("rate", 25_000_000),
            ("wfit", 24_000_000),
            ("wfit", 1_000_000_000),
        ):
            for mode, qemu in binaries.items():
                run_case(report, output, qemu, elfs[case], mode, case, frequency, token, environment)
        if (
            report["sha256"]["binaries"] != {mode: sha256(path) for mode, path in binaries.items()}
            or report["sha256"]["sources"] != {name: sha256(HERE / name) for name in SOURCES}
            or report["sha256"]["elfs"] != {case: sha256(path) for case, path in elfs.items()}
        ):
            raise FixtureError("binary, source, or fixture ELF changed during the run")
        report["status"] = "passed"
        exit_code = 0
    except KeyboardInterrupt:
        report.update(status="interrupted", error="interrupted by user")
        exit_code = 130
    except (FixtureError, OSError, subprocess.TimeoutExpired) as error:
        report.update(status="failed", error=str(error))
        print(f"error: {error}", file=sys.stderr)
    finally:
        (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"Results: {output / 'results.json'}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
