from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .applications import AppError, build_app, inspect_app
from .graphics import GRAPHICS_MODES
from .lifecycle import Device, Paths, Registry, Runtime, _validate_name
from .profiles import get_profile, list_profiles, public_metadata
from .service import DeviceError, DeviceService, device_metadata


def _print(value) -> None:
    print(json.dumps(value, indent=2, default=str, allow_nan=False))


def _tools_directory() -> Path:
    source = Path(__file__).resolve().parents[2] / "tools"
    return source if source.is_dir() else Path(__file__).parent / "_tools"


def _xtool_executable(paths: Paths, override: str | None) -> str:
    if override:
        return override
    configured = os.environ.get("LIMULATOR_XTOOL")
    if configured:
        return configured
    private = paths.prefix / "bin/xtool"
    return str(private) if os.access(private, os.X_OK) else "xtool"


def _build_project(paths: Paths, args: argparse.Namespace) -> Path:
    return build_app(
        args.project,
        _xtool_executable(paths, args.xtool),
        args.configuration,
        env=os.environ | {"LIMULATOR_HOME": str(paths.data)},
    )


def doctor(paths: Paths) -> dict:
    from .creator import _native_env

    environment = _native_env(paths)
    tools = {
        "qemu": paths.backend / "qemu/build/qemu-system-aarch64",
        "firmware_cli": Path(environment["VC"]),
        "firmware_release": Path(environment["VSETUP_RELEASE"]),
        "usbmuxd": paths.backend / ".build/usbmuxd-32k/usbmuxd",
        "apfs": paths.backend / ".build/linux-apfs-rw/apfs.ko",
    }
    checks = {name: path.exists() for name, path in tools.items() if name != "firmware_release"}
    resources = tools["firmware_release"] / "app/VPhone.bundle/Contents/Resources"
    checks["firmware_release"] = all(
        any(
            path.is_file() and path.stat().st_size > 0
            for path in (resources / name / f"{name}.bin", resources / f"{name}.bin")
        )
        for name in ("OpenAVB", "OpenSEP")
    )
    qemu_data_dirs = []
    try:
        qemu_data = subprocess.run(
            [str(tools["qemu"]), "-L", "help"],
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
            cwd=paths.backend,
        )
        if qemu_data.returncode == 0:
            for line in qemu_data.stdout.splitlines():
                if line.strip():
                    path = Path(line.strip())
                    qemu_data_dirs.append(path if path.is_absolute() else paths.backend / path)
    except (OSError, subprocess.TimeoutExpired):
        pass
    # The pinned virtio-net PCI device loads this ROM by default.
    qemu_romfile = None
    for directory in (paths.backend, *qemu_data_dirs):
        candidate = directory / "efi-virtio.rom"
        try:
            if candidate.is_file() and os.access(candidate, os.R_OK) and candidate.stat().st_size > 0:
                qemu_romfile = candidate
                break
        except OSError:
            continue
    checks["qemu_romfile"] = qemu_romfile is not None
    tools["qemu_romfile"] = qemu_romfile
    probes = {
        "qemu_machine": ([str(tools["qemu"]), "-machine", "help"], "vresearch101"),
        "gpu_device": ([str(tools["qemu"]), "-device", "apple-gfx-mmio,help"], "apple-gfx-mmio"),
        "firmware_cli_runs": ([str(tools["firmware_cli"]), "--help"], "vphone"),
    }
    for name, (command, expected) in probes.items():
        try:
            probe = subprocess.run(command, capture_output=True, text=True, timeout=20, env=environment)
            checks[name] = probe.returncode == 0 and expected in (probe.stdout + probe.stderr).lower()
        except (OSError, subprocess.TimeoutExpired):
            checks[name] = False
    if checks["apfs"] and shutil.which("modinfo"):
        probe = subprocess.run(
            ["modinfo", "-F", "vermagic", str(tools["apfs"])], capture_output=True, text=True, timeout=10
        )
        checks["kernel_module_matches"] = probe.returncode == 0 and probe.stdout.startswith(
            platform.release() + " "
        )
    checks["architecture"] = platform.machine() == "x86_64"
    checks["vsock"] = os.access("/dev/vhost-vsock", os.R_OK | os.W_OK)
    checks["systemd_user"] = (
        subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True, timeout=10
        ).returncode
        == 0
    )
    paths.data.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(paths.data)
    checks["disk"] = disk.free >= 60 * (1 << 30)
    gpu = "vulkaninfo is not installed"
    if shutil.which("vulkaninfo"):
        result = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=30)
        gpu = result.stdout.strip() if result.returncode == 0 else result.stderr.strip()
        checks["vulkan"] = result.returncode == 0
    else:
        checks["vulkan"] = False
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "tools": tools,
        "free_disk_gib": round(disk.free / (1 << 30), 1),
        "gpu": gpu,
        "data": paths.data,
    }


class _ConsoleService:
    def __init__(self, name: str, paths: Paths):
        _validate_name(name)
        self.name, self.paths = name, paths
        self._services: dict[str, DeviceService] = {}
        self._lock = threading.Lock()

    def for_device(self, name: str) -> DeviceService:
        with self._lock:
            device = Registry(self.paths).get(name)
            service = self._services.get(name)
            if service is None or service.runtime.device != device:
                service = DeviceService(Runtime(self.paths, device))
                self._services[name] = service
            return service

    def devices(self):
        return {
            "default_device": self.name,
            "devices": [device_metadata(device) for device in Registry(self.paths).list()],
            "profiles": [public_metadata(profile) for profile in list_profiles()],
        }

    def status(self):
        try:
            return self.for_device(self.name).status()
        except ValueError as error:
            if not str(error).startswith("unknown device:"):
                raise
        return {
            "name": self.name,
            "state": "unconfigured",
            "device": {},
            "backend": {"ready": False},
            "error": "No device is registered. Create or register a local iOS guest first.",
        }

    def __getattr__(self, name):
        def unavailable(*_args, **_kwargs):
            try:
                service = self.for_device(self.name)
            except ValueError as error:
                raise DeviceError(str(error)) from error
            return getattr(service, name)(*_args, **_kwargs)

        return unavailable


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Run and test iPhone apps on a local Linux iOS guest")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--data", type=Path, help="local runtime data directory")
    root.add_argument("--device", default="duo", help="registered device name (default: duo)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="check the host and backend tools")
    bootstrap = commands.add_parser("bootstrap", help="build the pinned backend or install xtool")
    bootstrap.add_argument("phase", nargs="?", default="all", help="backend phase, or xtool")
    commands.add_parser("devices", help="list registered guests")
    commands.add_parser("profiles", help="list available iPhone, iPad, and Duo profiles")
    graphics_help = (
        "compatible uses Reims's normal policy; strict rejects more differences; diagnostic enables a "
        "run-owned census. No mode establishes full Metal equivalence."
    )
    create = commands.add_parser("create", help="create a local iOS guest from Apple firmware")
    create.add_argument("--profile", help="device profile ID (default: iphone-duo)")
    create.add_argument("--model")
    create.add_argument("--ios")
    create.add_argument(
        "--cid", type=int, help="guest CID (default: reuse this device's CID or choose a free one)"
    )
    create.add_argument("--scale", type=int, choices=(1, 2, 3))
    create.add_argument(
        "--fresh-disk",
        action="store_true",
        help="recover a matching incomplete creation by preserving its disk and preparing a fresh one",
    )
    create.add_argument("--graphics-mode", choices=GRAPHICS_MODES, help=graphics_help)
    register = commands.add_parser("register", help="register an existing VM bundle")
    register.add_argument("vm", type=Path)
    register.add_argument("--model", required=True)
    register.add_argument("--ios", required=True)
    register.add_argument("--cid", type=int, default=41)
    register.add_argument("--width", type=int, default=1398)
    register.add_argument("--height", type=int, default=2034)
    register.add_argument("--scale", type=float, default=3)
    start = commands.add_parser("start", help="start the selected device")
    start.add_argument("--window", action="store_true", help="also open the native emulator window")
    start.add_argument("--wait", type=float, default=300)
    start.add_argument("--graphics-mode", choices=GRAPHICS_MODES, help=graphics_help)
    commands.add_parser("stop", help="shut down this exact guest")
    commands.add_parser("status", help="read live runtime status")
    build = commands.add_parser("build", help="build a SwiftPM iPhone app with xtool")
    run = commands.add_parser("run", help="xtool build, install, and launch an app")
    for command in (build, run):
        command.add_argument("project", type=Path)
        command.add_argument("--xtool", help="xtool executable (default: private installation, then PATH)")
        command.add_argument("--configuration", choices=("debug", "release"), default="debug")
    run.add_argument("--graphics-mode", choices=GRAPHICS_MODES, help=graphics_help)
    install = commands.add_parser("install", help="install a development .app or .ipa")
    install.add_argument("app", type=Path)
    launch = commands.add_parser("launch", help="launch an installed app")
    launch.add_argument("bundle_id")
    commands.add_parser("apps", help="list real installed guest applications")
    shot = commands.add_parser("screenshot", help="save a genuine guest framebuffer PNG")
    shot.add_argument("output", type=Path, nargs="?")
    tap = commands.add_parser("tap", help="tap a coordinate in the current screenshot")
    tap.add_argument("x", type=float)
    tap.add_argument("y", type=float)
    element = commands.add_parser("tap-element", help="tap a native accessibility identifier")
    element.add_argument("identifier")
    swipe = commands.add_parser("swipe", help="drag between screenshot coordinates")
    for coordinate in ("x0", "y0", "x1", "y1"):
        swipe.add_argument(coordinate, type=float)
    swipe.add_argument("--duration", type=float, default=350, help="duration in milliseconds")
    text = commands.add_parser("text", help="type into the focused native text field")
    text.add_argument("text")
    button = commands.add_parser("button", help="press a hardware button")
    button.add_argument("name", choices=("home", "lock", "volume-up", "volume-down"))
    fold = commands.add_parser("fold", help="change a Duo guest's posture")
    fold.add_argument("position", choices=("closed", "book", "open"))
    commands.add_parser("unlock", help="wake and unlock the guest")
    commands.add_parser("tree", help="read the native accessibility tree")
    scenario = commands.add_parser("test", help="run a JSON scenario against the real guest")
    scenario.add_argument("scenario", type=Path)
    scenario.add_argument("--output", type=Path, required=True)
    serve = commands.add_parser("serve", help="open the local interactive device console")
    serve.add_argument("--port", type=int, default=18765)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    paths = Paths(args.data) if args.data else Paths()
    registry = Registry(paths)
    try:
        if args.command == "doctor":
            result = doctor(paths)
            _print(result)
            return 0 if result["ready"] else 1
        if args.command == "bootstrap":
            command = (
                [str(_tools_directory() / "bootstrap_xtool.sh")]
                if args.phase == "xtool"
                else [str(_tools_directory() / "bootstrap_backend.sh"), args.phase]
            )
            return subprocess.run(
                command,
                env=os.environ | {"LIMULATOR_HOME": str(paths.data)},
            ).returncode
        if args.command == "devices":
            _print([asdict(device) for device in registry.list()])
            return 0
        if args.command == "profiles":
            _print([public_metadata(profile) for profile in list_profiles()])
            return 0
        if args.command == "create":
            from .creator import create_device

            profile = get_profile(args.profile or "iphone-duo")
            if args.profile:
                for option, expected in (
                    ("model", profile.model),
                    ("ios", profile.os_version),
                    ("scale", profile.scale),
                ):
                    value = getattr(args, option)
                    if value is not None and value != expected:
                        raise ValueError(f"--{option} contradicts profile {profile.id}: expected {expected}")
            options = {"fresh_disk": args.fresh_disk}
            if args.graphics_mode is not None:
                options["graphics_mode"] = args.graphics_mode
            _print(
                create_device(
                    paths,
                    args.device,
                    args.model or profile.model,
                    args.ios or profile.os_version,
                    args.cid,
                    args.scale,
                    **options,
                )
            )
            return 0
        if args.command == "register":
            device = Device(
                args.device, args.vm, args.model, args.ios, args.cid, args.width, args.height, args.scale
            )
            _print(asdict(registry.register(device)))
            return 0
        if args.command == "build":
            ipa = _build_project(paths, args)
            _print(asdict(inspect_app(ipa)))
            return 0
        if args.command == "serve":
            import uvicorn

            from .server import create_app

            service = _ConsoleService(args.device, paths)
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            print(f"Starting Limulator console at http://127.0.0.1:{args.port}", flush=True)
            uvicorn.run(
                create_app(service, port=args.port), host="127.0.0.1", port=args.port, log_level="warning"
            )
            return 0
        runtime = Runtime(paths, registry.get(args.device))
        service = DeviceService(runtime)
        if args.command == "start":
            options = {"window": args.window, "wait_timeout": args.wait}
            if args.graphics_mode is not None:
                options["graphics_mode"] = args.graphics_mode
            result = runtime.start(**options)
        elif args.command == "stop":
            result = runtime.stop()
        elif args.command == "status":
            result = service.status()
        elif args.command == "run":
            explicit_graphics_mode = args.graphics_mode is not None or "LIMULATOR_GRAPHICS_MODE" in os.environ
            if explicit_graphics_mode:
                runtime.require_graphics_mode(args.graphics_mode)
            ipa = _build_project(paths, args)
            if runtime.status()["state"] == "running":
                if explicit_graphics_mode:
                    runtime.require_graphics_mode(args.graphics_mode)
            else:
                options = {"wait_timeout": 300}
                if args.graphics_mode is not None:
                    options["graphics_mode"] = args.graphics_mode
                runtime.start(**options)
            installed = service.install(ipa)
            service.unlock()
            result = {"install": installed, "launch": service.launch(installed["bundle_id"])}
        elif args.command == "install":
            result = service.install(args.app)
        elif args.command == "launch":
            result = service.launch(args.bundle_id)
        elif args.command == "apps":
            result = service.apps()
        elif args.command == "screenshot":
            output = args.output or paths.captures / f"{args.device}-{time.time_ns()}.png"
            frame = service.capture(output)
            result = {
                "path": output.resolve(),
                "width": frame.width,
                "height": frame.height,
                "frame": frame.identity,
                "source": "guest framebuffer",
            }
        elif args.command == "tap":
            result = service.tap(args.x, args.y)
        elif args.command == "tap-element":
            result = service.tap_element(args.identifier)
        elif args.command == "swipe":
            result = service.swipe(args.x0, args.y0, args.x1, args.y1, args.duration)
        elif args.command == "text":
            result = service.text(args.text)
        elif args.command == "button":
            result = service.button(args.name)
        elif args.command == "fold":
            result = service.fold(args.position)
        elif args.command == "unlock":
            result = service.unlock()
        elif args.command == "tree":
            result = service.tree()
        elif args.command == "test":
            from .scenarios import run_scenario

            result = run_scenario(service, args.scenario, args.output)
        else:
            raise ValueError(f"unhandled command: {args.command}")
        _print(result)
        return 0
    except (OSError, RuntimeError, ValueError, AppError, subprocess.SubprocessError) as error:
        print(f"limulator: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
