"""Graphics policy selection for new guest launches."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

GRAPHICS_MODES = ("strict", "diagnostic")
DEFAULT_GRAPHICS_MODE = "strict"
GRAPHICS_MODE_ENV = "LIMULATOR_GRAPHICS_MODE"


def select_graphics_mode(value: str | None) -> tuple[str, bool]:
    """Return the requested mode and whether the caller selected it explicitly."""
    selected = value
    explicit = value is not None
    if selected is None and GRAPHICS_MODE_ENV in os.environ:
        selected = os.environ[GRAPHICS_MODE_ENV]
        explicit = True
    if selected is None:
        return DEFAULT_GRAPHICS_MODE, False
    if selected not in GRAPHICS_MODES:
        choices = " or ".join(GRAPHICS_MODES)
        raise ValueError(f"graphics mode must be {choices}; got {selected!r}")
    return selected, explicit


def apply_graphics_environment(env: dict[str, str], mode: str, census_dir: Path) -> None:
    """Set only the vGPU policy flags owned by Limulator."""
    if mode not in GRAPHICS_MODES:
        raise ValueError(f"unsupported graphics mode: {mode!r}")
    env["REIMS_VGPU_STRICT"] = "1" if mode == "strict" else "0"
    env["REIMS_VGPU_CENSUS_DIR"] = "" if mode == "strict" else os.fspath(census_dir)


def graphics_policy_metadata(mode: str, env: Mapping[str, str]) -> dict[str, object]:
    exact_set = "REIMS_VGPU_EXACT" in env
    return {
        "mode": mode,
        "reims_vgpu_exact": {
            "known": True,
            "set": exact_set,
            "value": env.get("REIMS_VGPU_EXACT"),
        },
    }


def recorded_graphics_mode(value: Mapping[str, object] | None) -> str:
    if not isinstance(value, Mapping):
        return "unknown"
    policy = value.get("graphics_policy")
    if not isinstance(policy, Mapping):
        return "unknown"
    mode = policy.get("mode")
    return mode if mode in GRAPHICS_MODES else "unknown"


def require_graphics_mode(name: str, actual: str, requested: str) -> None:
    if actual == requested:
        return
    raise RuntimeError(
        f"device {name!r} has graphics mode {actual}; cannot apply {requested} to an existing device. "
        f"Stop it with `limulator --device {name} stop`, then start it with "
        f"`limulator --device {name} start --graphics-mode {requested}`."
    )
