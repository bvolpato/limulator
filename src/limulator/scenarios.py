"""Run bounded, reproducible UI scenarios against Limulator's device service.

Scenario files are UTF-8 JSON objects with ``version: 1`` and a ``steps`` array.
Each step uses an ``action`` field and only the parameters documented by that action.
The runner writes an exclusive artifact directory containing ``report.json`` and
screenshots captured from the connected guest.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .protocols import ProtocolError

if TYPE_CHECKING:
    from .service import DeviceService, Frame


class ScenarioError(RuntimeError):
    """A scenario failed validation, execution, or evidence capture."""

    def __init__(self, message: str, *, report_path: Path | None = None) -> None:
        super().__init__(message)
        self.report_path = report_path


_MAX_SCENARIO_BYTES = 1 << 20
_MAX_STEPS = 100
_MAX_REPORT_RESULT_BYTES = 64 << 10
_MAX_TREE_NODES = 100_000
_WAIT_TEXT_POLL_SECONDS = 0.25
_WAIT_HINGE_POLL_SECONDS = 0.25
_MAX_AX_ERROR_BODY_BYTES = 4096
_AX_READINESS_ERRORS = frozenset(
    {
        ("failed", "AX element query failed (-25215)"),
        ("failed", "AX element query failed (-25216)"),
        (
            "unavailable",
            "Active interface geometry is unavailable: "
            "Foreground bundle or its unique scene could not be verified within the display bounds",
        ),
    }
)
_TEXT_FIELDS = {"label", "value", "title", "text", "accessibilitylabel", "name"}
_BUTTONS = {"home", "lock", "volume-up", "volume-down"}
_FOLD_POSITIONS = {"closed", "book", "open"}
_HINGE_TARGET_ANGLES = {"closed": 0.0, "book": 100.0, "open": 180.0}
_HINGE_STABLE_READBACKS = 3
_HINGE_HEARTBEAT_ADVANCES = 2
_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_SCREENSHOT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value} is not allowed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _read_scenario(path: Path) -> tuple[dict[str, Any], str]:
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_SCENARIO_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"cannot read scenario file: {exc}") from exc
    if len(data) > _MAX_SCENARIO_BYTES:
        raise ValueError(f"scenario JSON exceeds {_MAX_SCENARIO_BYTES} bytes")
    digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8")
        value = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid scenario JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("scenario must be a JSON object")
    return value, digest


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _string(value: Any, name: str, *, nonempty: bool = True, maximum: int = 4096) -> str:
    if not isinstance(value, str) or (nonempty and not value) or len(value) > maximum or "\0" in value:
        raise ValueError(f"{name} must be a string of at most {maximum} characters without NUL")
    return value


def _bundle_id(value: Any, field: str) -> str:
    result = _string(value, field, maximum=255)
    if not _BUNDLE_ID.fullmatch(result):
        raise ValueError(f"{field} must be a valid application bundle identifier")
    return result


def _screenshot_name(value: Any) -> str:
    name = _string(value, "screenshot name", maximum=128)
    if not _SCREENSHOT_NAME.fullmatch(name):
        raise ValueError("screenshot name must be a simple filename without path separators")
    if not name.casefold().endswith(".png"):
        name += ".png"
    return name


def _optional_identifier(step: dict[str, Any], index: int) -> str | None:
    if "identifier" not in step:
        return None
    return _string(step["identifier"], f"step {index} identifier", maximum=1024)


def _exact_match(step: dict[str, Any], index: int) -> bool:
    exact = step.get("exact", False)
    if not isinstance(exact, bool):
        raise ValueError(f"step {index} exact must be a boolean")
    return exact


def _validate_steps(document: dict[str, Any]) -> list[dict[str, Any]]:
    unknown = set(document) - {"version", "steps"}
    if unknown:
        raise ValueError(f"unknown scenario fields: {', '.join(sorted(unknown))}")
    if type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError("scenario version must be the integer 1")
    source_steps = document.get("steps")
    if not isinstance(source_steps, list) or not source_steps:
        raise ValueError("scenario steps must be a non-empty array")
    if len(source_steps) > _MAX_STEPS:
        raise ValueError(f"scenario has more than {_MAX_STEPS} steps")

    normalized: list[dict[str, Any]] = []
    screenshot_names: set[str] = {"failure.png"}
    planned_names: list[str] = []
    for index, step in enumerate(source_steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"step {index} must be an object")
        if not isinstance(step.get("action"), str):
            raise ValueError(f"step {index} is missing a string action")
        action = step["action"]
        common = {"action"}

        if action == "launch":
            required, optional = {"bundle_id"}, {"args"}
            call = {
                "bundle_id": _bundle_id(step.get("bundle_id"), f"step {index} bundle_id"),
                "args": None,
            }
            if "args" in step:
                args = step["args"]
                if not isinstance(args, list) or len(args) > 64:
                    raise ValueError(f"step {index} args must be an array of at most 64 strings")
                call["args"] = [_string(value, f"step {index} args item", maximum=4096) for value in args]
                if call["args"]:
                    raise ValueError(
                        f"step {index} non-empty launch args are unsupported by the selected backend"
                    )
        elif action == "tap_element":
            required, optional = {"identifier"}, set()
            call = {"identifier": _string(step.get("identifier"), f"step {index} identifier", maximum=1024)}
        elif action == "scroll_to_element":
            required, optional = {"identifier"}, {"direction", "max_swipes", "timeout"}
            direction = step.get("direction", "down")
            if direction not in ("down", "up"):
                raise ValueError(f"step {index} scroll direction must be down or up")
            max_swipes = step.get("max_swipes", 6)
            if type(max_swipes) is not int or not 0 <= max_swipes <= 12:
                raise ValueError(f"step {index} max_swipes must be an integer between 0 and 12")
            timeout = _number(step.get("timeout", 30), f"step {index} timeout")
            if not 0 < timeout <= 120:
                raise ValueError(
                    f"step {index} scroll timeout must be greater than 0 and at most 120 seconds"
                )
            call = {
                "identifier": _string(step.get("identifier"), f"step {index} identifier", maximum=1024),
                "direction": direction,
                "max_swipes": max_swipes,
                "timeout": timeout,
            }
        elif action == "tap":
            required, optional = {"x", "y"}, set()
            call = {key: _number(step.get(key), f"step {index} {key}") for key in ("x", "y")}
        elif action == "swipe":
            required, optional = {"x0", "y0", "x1", "y1"}, {"duration_ms"}
            call = {key: _number(step.get(key), f"step {index} {key}") for key in ("x0", "y0", "x1", "y1")}
            duration = _number(step.get("duration_ms", 350), f"step {index} duration_ms")
            if not 50 <= duration <= 5000:
                raise ValueError(f"step {index} duration_ms must be between 50 and 5000")
            call["duration_ms"] = duration
        elif action == "text":
            required, optional = {"text"}, set()
            call = {"text": _string(step.get("text"), f"step {index} text")}
        elif action == "button":
            required, optional = {"name"}, set()
            name = _string(step.get("name"), f"step {index} button name", maximum=64)
            if name not in _BUTTONS:
                raise ValueError(f"step {index} button name must be one of {', '.join(sorted(_BUTTONS))}")
            call = {"name": name}
        elif action == "fold":
            required, optional = {"position"}, {"timeout"}
            position = _string(step.get("position"), f"step {index} fold position", maximum=16)
            if position not in _FOLD_POSITIONS:
                raise ValueError(
                    f"step {index} fold position must be one of {', '.join(sorted(_FOLD_POSITIONS))}"
                )
            timeout = _number(step.get("timeout", 30), f"step {index} timeout")
            if not 0 < timeout <= 120:
                raise ValueError(f"step {index} fold timeout must be greater than 0 and at most 120 seconds")
            call = {"position": position, "timeout": timeout}
        elif action == "capture_hinge_baseline":
            required, optional = {"position"}, set()
            position = _string(step.get("position"), f"step {index} hinge position", maximum=16)
            if position not in _HINGE_TARGET_ANGLES:
                raise ValueError(
                    f"step {index} hinge position must be one of {', '.join(sorted(_HINGE_TARGET_ANGLES))}"
                )
            call = {"position": position}
        elif action == "wait_hinge":
            required, optional = {"position"}, {"timeout"}
            position = _string(step.get("position"), f"step {index} hinge position", maximum=16)
            if position not in _HINGE_TARGET_ANGLES:
                raise ValueError(
                    f"step {index} hinge position must be one of {', '.join(sorted(_HINGE_TARGET_ANGLES))}"
                )
            timeout = _number(step.get("timeout", 30), f"step {index} timeout")
            if not 0 < timeout <= 120:
                raise ValueError(
                    f"step {index} hinge timeout must be greater than 0 and at most 120 seconds"
                )
            call = {"position": position, "timeout": timeout}
        elif action == "terminate":
            required, optional = {"bundle_id"}, set()
            call = {"bundle_id": _bundle_id(step.get("bundle_id"), f"step {index} bundle_id")}
        elif action == "screenshot":
            required, optional = {"name"}, set()
            call = {"name": _screenshot_name(step.get("name"))}
        elif action == "wait_text":
            required, optional = {"text"}, {"timeout", "identifier", "exact"}
            query = _string(step.get("text"), f"step {index} text")
            timeout = _number(step.get("timeout", 30), f"step {index} timeout")
            if not 0 <= timeout <= 120:
                raise ValueError(f"step {index} timeout must be between 0 and 120 seconds")
            call = {
                "text": query,
                "timeout": timeout,
                "identifier": _optional_identifier(step, index),
                "exact": _exact_match(step, index),
            }
        elif action == "assert_text":
            required, optional = {"text"}, {"identifier", "exact"}
            call = {
                "text": _string(step.get("text"), f"step {index} text"),
                "identifier": _optional_identifier(step, index),
                "exact": _exact_match(step, index),
            }
        elif action == "wait":
            required, optional = {"seconds"}, set()
            seconds = _number(step.get("seconds"), f"step {index} seconds")
            if not 0 <= seconds <= 30:
                raise ValueError(f"step {index} seconds must be between 0 and 30")
            call = {"seconds": seconds}
        else:
            raise ValueError(f"step {index} has unknown action: {action}")

        allowed = common | required | optional
        unknown_step = set(step) - allowed
        missing = required - set(step)
        if unknown_step:
            raise ValueError(f"step {index} has unknown fields: {', '.join(sorted(unknown_step))}")
        if missing:
            raise ValueError(f"step {index} is missing fields: {', '.join(sorted(missing))}")
        normalized_step = {"action": action, **call}
        normalized.append(normalized_step)
        if action == "screenshot":
            planned_names.append(call["name"])
        else:
            planned_names.append(f"step-{index:03}.png")

    for name in planned_names:
        folded = name.casefold()
        if folded in screenshot_names:
            raise ValueError(f"duplicate or reserved screenshot name: {name}")
        screenshot_names.add(folded)
    return normalized


def _text_matches(value: str, expected: str, exact: bool) -> bool:
    return value == expected if exact else expected.casefold() in value.casefold()


def _visible_matches(
    tree: Any,
    expected: str,
    *,
    identifier: str | None = None,
    exact: bool = False,
) -> list[str]:
    """Find text fields, optionally scoped to the direct fields of one AX element."""
    matches: list[str] = []
    seen: set[str] = set()

    if identifier is not None:
        stack: list[Any] = [tree]
        visited = 0
        while stack and visited < _MAX_TREE_NODES:
            value = stack.pop()
            visited += 1
            if isinstance(value, dict):
                if "identifier" in value:
                    if value.get("identifier") != identifier:
                        continue
                    for key, child in value.items():
                        field_name = key.casefold() if isinstance(key, str) else None
                        if (
                            field_name in _TEXT_FIELDS
                            and isinstance(child, str)
                            and _text_matches(child, expected, exact)
                            and child not in seen
                        ):
                            matches.append(child[:512])
                            seen.add(child)
                            if len(matches) >= 100:
                                return matches
                    continue
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        if visited + len(stack) >= _MAX_TREE_NODES:
                            return matches
                        stack.append(child)
            elif isinstance(value, list):
                for child in reversed(value):
                    if isinstance(child, (dict, list)):
                        if visited + len(stack) >= _MAX_TREE_NODES:
                            return matches
                        stack.append(child)
        return matches

    stack: list[tuple[Any, str | None]] = [(tree, None)]
    visited = 0
    while stack and visited < _MAX_TREE_NODES:
        value, field_name = stack.pop()
        visited += 1
        if isinstance(value, dict):
            for key, child in value.items():
                visited += 1
                if visited >= _MAX_TREE_NODES:
                    return matches
                child_field = key.casefold() if isinstance(key, str) else None
                if (
                    child_field in _TEXT_FIELDS
                    and isinstance(child, str)
                    and _text_matches(child, expected, exact)
                    and child not in seen
                ):
                    matches.append(child[:512])
                    seen.add(child)
                    if len(matches) >= 100:
                        return matches
                if isinstance(child, (dict, list)) or (
                    isinstance(child, str) and child_field in _TEXT_FIELDS
                ):
                    if visited + len(stack) >= _MAX_TREE_NODES:
                        return matches
                    stack.append((child, child_field))
        elif isinstance(value, list):
            for child in reversed(value):
                if isinstance(child, (dict, list)) or (isinstance(child, str) and field_name in _TEXT_FIELDS):
                    if visited + len(stack) >= _MAX_TREE_NODES:
                        return matches
                    stack.append((child, field_name))
        elif isinstance(value, str) and field_name in _TEXT_FIELDS and _text_matches(value, expected, exact):
            if value not in seen:
                matches.append(value[:512])
                seen.add(value)
                if len(matches) >= 100:
                    return matches
    return matches


def _ax_readiness_error(error: ProtocolError) -> str | None:
    if error.status_code != 400 or not isinstance(error.details, bytes):
        return None
    if len(error.details) > _MAX_AX_ERROR_BODY_BYTES:
        return None
    try:
        response = json.loads(
            error.details.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (RecursionError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(response, dict):
        return None
    detail = response.get("error")
    if not isinstance(detail, dict):
        return None
    code = detail.get("code")
    message = detail.get("message")
    return (
        message
        if isinstance(code, str) and isinstance(message, str) and (code, message) in _AX_READINESS_ERRORS
        else None
    )


def _safe_result(value: Any) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError):
        return {"repr": repr(value)[:2048], "serializable": False}
    if len(encoded) > _MAX_REPORT_RESULT_BYTES:
        return {
            "truncated": True,
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "preview": encoded[:2048].decode("utf-8", "replace"),
        }
    return value


def _file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _capture(
    service: DeviceService,
    screenshots_dir: Path,
    name: str,
) -> dict[str, Any]:
    path = screenshots_dir / name
    try:
        frame: Frame = service.capture(path)
        if not isinstance(frame.png, bytes) or not frame.png:
            raise ScenarioError("device capture did not return PNG bytes")
        if (
            isinstance(frame.width, bool)
            or isinstance(frame.height, bool)
            or not isinstance(frame.width, int)
            or not isinstance(frame.height, int)
            or frame.width <= 0
            or frame.height <= 0
            or not isinstance(frame.identity, str)
            or not frame.identity
        ):
            raise ScenarioError("device capture returned invalid frame metadata")
        if not path.is_file():
            raise ScenarioError("device capture did not write the requested screenshot")
        digest, size = _file_sha256(path)
        expected_digest = hashlib.sha256(frame.png).hexdigest()
        if size != len(frame.png) or digest != expected_digest:
            raise ScenarioError("captured screenshot file does not match the returned guest frame")
        return {
            "path": f"screenshots/{name}",
            "sha256": digest,
            "width": frame.width,
            "height": frame.height,
            "identity": frame.identity,
        }
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _write_report(output_dir: Path, report: dict[str, Any]) -> Path:
    report_path = output_dir / "report.json"
    data = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8") + b"\n"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output_dir, prefix=".report-", delete=False
        ) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, report_path)
    except OSError:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        raise
    return report_path


def _finish_failure(
    service: DeviceService,
    output_dir: Path,
    screenshots_dir: Path | None,
    report: dict[str, Any],
    error: Exception,
    *,
    failed_step: dict[str, Any] | None = None,
    step_index: int | None = None,
) -> ScenarioError:
    report["status"] = "failed"
    report["failure"] = {
        "step": step_index,
        "error": str(error) or error.__class__.__name__,
        "type": error.__class__.__name__,
    }
    if screenshots_dir is None:
        report["failure_screenshot_error"] = "screenshot directory could not be created"
    else:
        try:
            screenshot = _capture(service, screenshots_dir, "failure.png")
        except Exception as capture_error:
            report["failure_screenshot_error"] = str(capture_error) or capture_error.__class__.__name__
        else:
            screenshot["role"] = "failure"
            report["screenshots"].append(screenshot)
            if failed_step is not None:
                failed_step["screenshots"].append(screenshot)
            else:
                report["failure_screenshots"] = [screenshot]
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["duration_seconds"] = max(0.0, time.monotonic() - report["_started_monotonic"])
    report.pop("_started_monotonic", None)
    report_path = output_dir / "report.json"
    try:
        _write_report(output_dir, report)
    except Exception as report_error:
        return ScenarioError(
            f"{report['failure']['error']}; could not write failure report: {report_error}",
            report_path=report_path,
        )
    return ScenarioError(f"{report['failure']['error']} (report: {report_path})", report_path=report_path)


def _ax_element_is_visible(value: Any) -> bool:
    """Accept Boolean visibility and vphoned's numeric 1/0 representation."""
    return value is True or (type(value) is int and value == 1)


def _scroll_target(tree: Any, identifier: str, width: float, height: float) -> tuple[dict | None, str]:
    if isinstance(tree, dict) and tree.get("error"):
        raise ScenarioError(f"Guest accessibility query failed: {tree['error']}")
    if not isinstance(tree, dict) or tree.get("source") != "ax" or not isinstance(tree.get("elements"), list):
        raise ScenarioError("scroll_to_element requires the guest's flat AX elements snapshot")
    if tree.get("truncated") is True:
        raise ScenarioError(
            "Guest accessibility query was truncated; refusing to scroll with incomplete evidence"
        )
    matches = []
    for element in tree["elements"]:
        if not isinstance(element, dict):
            raise ScenarioError("Guest accessibility query returned a non-object element")
        if element.get("identifier") == identifier and _ax_element_is_visible(element.get("visible")):
            matches.append(element)
    if len(matches) > 1:
        raise ScenarioError(f"Accessibility identifier is ambiguous: {identifier!r}")
    if not matches:
        return None, "target is not visible"
    target = matches[0]
    try:
        x, y = _number(target.get("x"), "AX activation x"), _number(target.get("y"), "AX activation y")
    except ValueError as error:
        raise ScenarioError(
            f"Guest returned an invalid activation point for {identifier!r}: {error}"
        ) from error
    if not (0 <= x < width and 0 <= y < height):
        return None, f"activation point ({x:g}, {y:g}) is outside {width:g} by {height:g} guest points"
    return {"x": x, "y": y, "units": "points"}, "target is visible"


def _scroll_to_element(service: DeviceService, action: dict[str, Any]) -> dict:
    started = time.monotonic()
    deadline = started + action["timeout"]
    swipes = 0
    reason = "target has not been queried"
    last_readiness_error: ProtocolError | None = None

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise ScenarioError(
                f"scroll_to_element {action['identifier']!r} timed out after {swipes} "
                f"{action['direction']} swipes: {reason}"
            ) from last_readiness_error
        return value

    while True:
        screen = service.screen(timeout=remaining())
        if not isinstance(screen, dict):
            raise ScenarioError("Guest device.screen response must be an object")
        try:
            width, height = (
                _number(screen.get("width"), "guest point width"),
                _number(screen.get("height"), "guest point height"),
            )
        except ValueError as error:
            raise ScenarioError(f"Guest returned invalid screen bounds: {error}") from error
        if width <= 0 or height <= 0:
            raise ScenarioError("Guest screen point bounds must be positive")
        try:
            tree = service.tree(timeout=remaining())
        except ProtocolError as error:
            readiness = _ax_readiness_error(error)
            if readiness is None:
                raise
            last_readiness_error = error
            reason = f"last AX readiness error: {readiness}"
            time.sleep(min(_WAIT_TEXT_POLL_SECONDS, remaining()))
            continue
        last_readiness_error = None
        target, reason = _scroll_target(tree, action["identifier"], width, height)
        remaining()
        if target:
            return {
                "identifier": action["identifier"],
                "direction": action["direction"],
                "swipes": swipes,
                "activation_point": target,
                "screen_points": {"width": width, "height": height},
                "elapsed_seconds": max(0.0, time.monotonic() - started),
            }
        if swipes >= action["max_swipes"]:
            raise ScenarioError(
                f"scroll_to_element {action['identifier']!r} exhausted {swipes} "
                f"{action['direction']} swipes: {reason}"
            )
        frame = service.capture()
        remaining()
        if (
            type(frame.width) is not int
            or type(frame.height) is not int
            or frame.width <= 0
            or frame.height <= 0
            or not isinstance(frame.identity, str)
            or not frame.identity
        ):
            raise ScenarioError("Guest capture returned invalid viewport geometry for scrolling")
        x = (frame.width - 1) * 0.5
        lower, upper = (frame.height - 1) * 0.78, (frame.height - 1) * 0.32
        y0, y1 = (lower, upper) if action["direction"] == "down" else (upper, lower)
        service.swipe(x, y0, x, y1, 350, frame.identity)
        swipes += 1
        time.sleep(min(0.25, remaining()))


def _hinge_number(value: Any, name: str) -> float:
    try:
        return _number(value, name)
    except ValueError as error:
        raise ScenarioError(f"Guest returned invalid hinge status: {error}") from error


def _hinge_flag(value: Any, name: str) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise ScenarioError(f"Guest returned an invalid hinge {name} flag")


def _hinge_status_fields(status: Any) -> dict[str, Any]:
    if not isinstance(status, dict):
        raise ScenarioError("Guest returned an invalid hinge status value")
    enumerated = _hinge_flag(status.get("enumerated"), "enumerated")
    sweeping = _hinge_flag(status.get("sweeping"), "sweeping")
    pid = status.get("pid")
    service_id = status.get("service_id")
    failures = status.get("dispatch_failures")
    error = status.get("error")
    if type(pid) is not int or pid <= 0:
        raise ScenarioError("Guest returned an invalid hinge provider PID")
    if type(service_id) is not int or service_id <= 0:
        raise ScenarioError("Guest returned an invalid hinge service ID")
    if type(failures) is not int or failures < 0:
        raise ScenarioError("Guest returned an invalid hinge dispatch-failure count")
    if not isinstance(error, str) or "\0" in error:
        raise ScenarioError("Guest returned an invalid hinge provider error")

    updated_at = _hinge_number(status.get("updated_at"), "updated_at")
    angle = _hinge_number(status.get("current_angle"), "current_angle")
    configuration = status.get("configuration")
    if not isinstance(configuration, dict):
        raise ScenarioError("Guest returned an invalid hinge configuration")
    enabled = _hinge_flag(configuration.get("enabled"), "configuration enabled")
    configured_angle = _hinge_number(configuration.get("angle"), "configuration.angle")
    if not 0 <= angle <= 180 or not 0 <= configured_angle <= 180:
        raise ScenarioError("Guest returned a hinge angle outside 0 through 180 degrees")
    if updated_at <= 0:
        raise ScenarioError("Guest returned an invalid hinge status timestamp")
    return {
        "enumerated": enumerated,
        "sweeping": sweeping,
        "pid": pid,
        "service_id": service_id,
        "dispatch_failures": failures,
        "error": error,
        "updated_at": updated_at,
        "current_angle": angle,
        "configuration_enabled": enabled,
        "configured_angle": configured_angle,
    }


def _capture_hinge_baseline(service: DeviceService) -> dict[str, Any]:
    status = service.hinge_status()
    if status is None:
        raise ScenarioError("cannot capture a pre-fold hinge baseline: the provider status is absent")
    fields = _hinge_status_fields(status)
    if (
        not fields["enumerated"]
        or fields["sweeping"]
        or fields["error"]
        or not fields["configuration_enabled"]
        or abs(fields["configured_angle"] - fields["current_angle"]) > 1
    ):
        raise ScenarioError(
            "cannot capture a pre-fold hinge baseline until the provider reports an enumerated, "
            "settled, error-free posture"
        )
    return fields


def _wait_hinge(
    service: DeviceService,
    action: dict[str, Any],
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + action["timeout"]
    target = _HINGE_TARGET_ANGLES[action["position"]]
    failure_baseline_scope = "pre_fold" if baseline is not None else "wait_window"
    baseline_fields = baseline
    first_updated_at = baseline["updated_at"] if baseline is not None else None
    previous_updated_at = first_updated_at
    healthy_heartbeat_advances = 0
    healthy_first_updated_at: float | None = None
    previous_healthy_updated_at: float | None = None
    stable_readbacks = 0
    readbacks = 0
    last_summary = "provider status is not available"

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise ScenarioError(
                f"timed out waiting for a fresh settled hinge status at {target:g} degrees "
                f"for {action['position']!r} after {readbacks} readbacks; {last_summary}"
            )
        return value

    while True:
        status = service.hinge_status(timeout=remaining())
        readbacks += 1
        now = time.monotonic()
        if now >= deadline:
            raise ScenarioError(
                f"timed out waiting for a fresh settled hinge status at {target:g} degrees "
                f"for {action['position']!r} after {readbacks} readbacks; {last_summary}"
            )
        if status is None:
            stable_readbacks = 0
            healthy_heartbeat_advances = 0
            healthy_first_updated_at = None
            previous_healthy_updated_at = None
            last_summary = "the hinge provider has not published a status value"
            time.sleep(min(_WAIT_HINGE_POLL_SECONDS, deadline - now))
            continue

        fields = _hinge_status_fields(status)
        if baseline_fields is None:
            baseline_fields = fields
            first_updated_at = fields["updated_at"]
            previous_updated_at = fields["updated_at"]
        else:
            if fields["pid"] != baseline_fields["pid"] or fields["service_id"] != baseline_fields["service_id"]:
                raise ScenarioError("hinge provider PID or service ID changed during the measured fold")
            if fields["dispatch_failures"] != baseline_fields["dispatch_failures"]:
                change = (
                    "increased"
                    if fields["dispatch_failures"] > baseline_fields["dispatch_failures"]
                    else "decreased"
                )
                raise ScenarioError(
                    f"hinge provider dispatch_failures {change} from the {failure_baseline_scope} "
                    f"baseline ({baseline_fields['dispatch_failures']} to {fields['dispatch_failures']})"
                )
        if first_updated_at is None:
            first_updated_at = fields["updated_at"]
        if previous_updated_at is not None and fields["updated_at"] < previous_updated_at:
            raise ScenarioError("Guest hinge status updated_at moved backwards")
        previous_updated_at = fields["updated_at"]

        healthy = (
            fields["enumerated"]
            and not fields["sweeping"]
            and fields["error"] == ""
            and fields["configuration_enabled"]
            and abs(fields["configured_angle"] - target) <= 1
            and abs(fields["current_angle"] - target) <= 1
        )
        if healthy:
            if healthy_first_updated_at is None:
                healthy_first_updated_at = fields["updated_at"]
            if (
                previous_healthy_updated_at is not None
                and fields["updated_at"] > previous_healthy_updated_at
            ):
                healthy_heartbeat_advances += 1
            previous_healthy_updated_at = fields["updated_at"]
            stable_readbacks += 1
        else:
            healthy_first_updated_at = None
            previous_healthy_updated_at = None
            healthy_heartbeat_advances = 0
            stable_readbacks = 0
        last_summary = (
            f"angle={fields['current_angle']:g}, enumerated={fields['enumerated']}, "
            f"pid={fields['pid']}, service_id={fields['service_id']}, "
            f"sweeping={fields['sweeping']}, error={fields['error']!r}, "
            f"stable_readbacks={stable_readbacks}, "
            f"healthy_heartbeat_advances={healthy_heartbeat_advances}"
        )
        if (
            first_updated_at is not None
            and fields["updated_at"] > first_updated_at
            and healthy_first_updated_at is not None
            and healthy_heartbeat_advances >= _HINGE_HEARTBEAT_ADVANCES
            and stable_readbacks >= _HINGE_STABLE_READBACKS
        ):
            assert baseline_fields is not None
            return {
                "position": action["position"],
                "target_angle": target,
                "current_angle": fields["current_angle"],
                "enumerated": fields["enumerated"],
                "pid": fields["pid"],
                "service_id": fields["service_id"],
                "sweeping": fields["sweeping"],
                "baseline_dispatch_failures": baseline_fields["dispatch_failures"],
                "dispatch_failures": fields["dispatch_failures"],
                "dispatch_failure_baseline": failure_baseline_scope,
                "stable_readbacks": stable_readbacks,
                "heartbeat_advances": healthy_heartbeat_advances,
                "elapsed_seconds": max(0.0, now - started),
            }
        time.sleep(min(_WAIT_HINGE_POLL_SECONDS, deadline - now))


def _execute_action(
    service: DeviceService,
    action: dict[str, Any],
    screenshots_dir: Path,
    context: dict[str, Any] | None = None,
) -> tuple[Any, str | None]:
    kind = action["action"]
    context = context if context is not None else {}
    if kind == "launch":
        return service.launch(action["bundle_id"], action["args"]), None
    if kind == "tap_element":
        return service.tap_element(action["identifier"]), None
    if kind == "scroll_to_element":
        return _scroll_to_element(service, action), None
    if kind == "tap":
        return service.tap(action["x"], action["y"]), None
    if kind == "swipe":
        return (
            service.swipe(
                action["x0"],
                action["y0"],
                action["x1"],
                action["y1"],
                action["duration_ms"],
            ),
            None,
        )
    if kind == "text":
        return service.text(action["text"]), None
    if kind == "button":
        return service.button(action["name"]), None
    if kind == "capture_hinge_baseline":
        pending = context.get("hinge_baseline")
        if pending and pending["applied"]:
            raise ScenarioError("wait_hinge must consume the previous fold baseline before another baseline")
        fields = _capture_hinge_baseline(service)
        context["hinge_baseline"] = {
            "position": action["position"],
            "fields": fields,
            "applied": False,
        }
        return {
            "position": action["position"],
            "current_angle": fields["current_angle"],
            "pid": fields["pid"],
            "service_id": fields["service_id"],
            "dispatch_failures": fields["dispatch_failures"],
        }, None
    if kind == "fold":
        if not service.supports_fold:
            return service.fold(action["position"]), None
        pending = context.get("hinge_baseline")
        baseline = None
        if pending:
            if pending["applied"]:
                raise ScenarioError("wait_hinge must consume the previous fold baseline before another fold")
            if pending["position"] != action["position"]:
                raise ScenarioError("pre-fold hinge baseline position does not match the fold action")
            pending["applied"] = True
            baseline = pending["fields"]
        else:
            baseline = _capture_hinge_baseline(service)
        result = service.fold(action["position"])
        settlement = _wait_hinge(
            service,
            {"position": action["position"], "timeout": action["timeout"]},
            baseline,
        )
        context.pop("hinge_baseline", None)
        return {"fold": result, "hinge_settled": settlement}, None
    if kind == "wait_hinge":
        pending = context.get("hinge_baseline")
        baseline = None
        if pending:
            if not pending["applied"]:
                raise ScenarioError("pre-fold hinge baseline must be followed by its fold before wait_hinge")
            if pending["position"] != action["position"]:
                raise ScenarioError("pre-fold hinge baseline position does not match wait_hinge")
            baseline = pending["fields"]
            context.pop("hinge_baseline")
        return _wait_hinge(service, action, baseline), None
    if kind == "terminate":
        return service.terminate(action["bundle_id"]), None
    if kind == "screenshot":
        return {"captured": True}, action["name"]
    if kind == "wait_text":
        started = time.monotonic()
        deadline = started + action["timeout"]
        last_ax_error: str | None = None

        def timeout_error() -> ScenarioError:
            target = f" in AX element {action['identifier']!r}" if action["identifier"] else ""
            mode = "exactly " if action["exact"] else ""
            message = f"timed out waiting for visible text {mode}{action['text']!r}{target}"
            if last_ax_error is not None:
                message += f"; last AX readiness error: {last_ax_error}"
            return ScenarioError(message)

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_error()
            try:
                tree = service.tree(timeout=remaining)
            except ProtocolError as exc:
                last_ax_error = _ax_readiness_error(exc)
                if last_ax_error is None:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise timeout_error()
                time.sleep(min(_WAIT_TEXT_POLL_SECONDS, remaining))
                continue
            matches = _visible_matches(
                tree,
                action["text"],
                identifier=action["identifier"],
                exact=action["exact"],
            )
            now = time.monotonic()
            if now >= deadline:
                raise timeout_error()
            if matches:
                return {
                    "text": action["text"],
                    "identifier": action["identifier"],
                    "exact": action["exact"],
                    "matches": matches,
                    "elapsed_seconds": max(0.0, now - started),
                }, None
            remaining = deadline - now
            if remaining <= 0:
                raise timeout_error()
            time.sleep(min(_WAIT_TEXT_POLL_SECONDS, remaining))
    if kind == "assert_text":
        matches = _visible_matches(
            service.tree(),
            action["text"],
            identifier=action["identifier"],
            exact=action["exact"],
        )
        if not matches:
            target = f" in AX element {action['identifier']!r}" if action["identifier"] else ""
            mode = "exactly " if action["exact"] else ""
            raise ScenarioError(f"expected visible text {mode}not found{target}: {action['text']!r}")
        return {
            "text": action["text"],
            "identifier": action["identifier"],
            "exact": action["exact"],
            "matches": matches,
        }, None
    if kind == "wait":
        started = time.monotonic()
        time.sleep(action["seconds"])
        return {"requested_seconds": action["seconds"], "elapsed_seconds": time.monotonic() - started}, None
    raise AssertionError(f"validated action was not implemented: {kind}")


def run_scenario(service: DeviceService, scenario_path: Path, output: Path) -> dict[str, Any]:
    """Validate and run a JSON v1 scenario, returning its written report."""
    scenario_path = Path(scenario_path)
    output_dir = Path(output).expanduser().absolute()
    try:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise ScenarioError(f"cannot create scenario output directory {output_dir}: {exc}") from exc
    screenshots_dir = output_dir / "screenshots"
    report: dict[str, Any] = {
        "version": 1,
        "scenario": scenario_path.name,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": [],
        "screenshots": [],
        "graphics_policy": {"mode": "unknown", "reims_vgpu_exact": {"known": False}},
        "_started_monotonic": time.monotonic(),
    }
    try:
        screenshots_dir.mkdir(mode=0o700)
    except OSError as exc:
        error = ValueError(f"cannot create screenshot directory: {exc}")
        raise _finish_failure(service, output_dir, None, report, error) from exc

    try:
        document, input_digest = _read_scenario(scenario_path)
        actions = _validate_steps(document)
        report["scenario_sha256"] = input_digest
        report["step_count"] = len(actions)
        report["graphics_policy"] = service.status()["graphics_policy"]
    except Exception as exc:
        if "scenario_sha256" not in report:
            report["scenario_sha256"] = None
        raise _finish_failure(service, output_dir, screenshots_dir, report, exc) from exc

    action_context: dict[str, Any] = {}
    for index, action in enumerate(actions, start=1):
        started = time.monotonic()
        record: dict[str, Any] = {
            "index": index,
            "action": action["action"],
            "parameters": {key: value for key, value in action.items() if key != "action"},
            "status": "running",
            "duration_seconds": 0.0,
            "result": None,
            "screenshots": [],
        }
        report["steps"].append(record)
        try:
            result, explicit_screenshot = _execute_action(service, action, screenshots_dir, action_context)
            if action["action"] != "screenshot":
                record["result"] = _safe_result(result)
            screenshot_name = explicit_screenshot or f"step-{index:03}.png"
            screenshot = _capture(service, screenshots_dir, screenshot_name)
            screenshot["step"] = index
            screenshot["role"] = "action"
            if action["action"] == "screenshot":
                record["result"] = {"captured": True}
            record["screenshots"].append(screenshot)
            report["screenshots"].append(screenshot)
            record["status"] = "passed"
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc) or exc.__class__.__name__
            record["duration_seconds"] = max(0.0, time.monotonic() - started)
            scenario_error = _finish_failure(
                service,
                output_dir,
                screenshots_dir,
                report,
                exc,
                failed_step=record,
                step_index=index,
            )
            raise scenario_error from exc
        record["duration_seconds"] = max(0.0, time.monotonic() - started)

    report["status"] = "passed"
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["duration_seconds"] = max(0.0, time.monotonic() - report.pop("_started_monotonic"))
    try:
        _write_report(output_dir, report)
    except Exception as exc:
        raise ScenarioError(
            f"scenario passed but its report could not be written: {exc}",
            report_path=output_dir / "report.json",
        ) from exc
    return report
