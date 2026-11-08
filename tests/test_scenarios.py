from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from limulator import scenarios
from limulator.protocols import ProtocolError
from limulator.scenarios import ScenarioError, run_scenario


class FakeFrame:
    png = b"fake guest frame png"
    width = 200
    height = 300
    identity = "200x300:0"


class FakeService:
    def __init__(
        self,
        *,
        trees: list[Any] | None = None,
        fail_action: str | None = None,
        fail_capture: bool = False,
        tree_delay: float = 0,
        screen: dict[str, Any] | None = None,
    ) -> None:
        self.inputs: list[tuple[str, tuple[Any, ...]]] = []
        self.capture_paths: list[Path | None] = []
        self.trees = trees or [{"label": "Counter"}]
        self.tree_calls = 0
        self.tree_timeouts: list[float] = []
        self.tree_delay = tree_delay
        self.fail_action = fail_action
        self.fail_capture = fail_capture
        self.screen_info = screen if screen is not None else {"width": 100, "height": 150, "scale": 2}
        self.screen_timeouts: list[float] = []

    def status(self) -> dict[str, Any]:
        return {"graphics_policy": {"mode": "unknown", "reims_vgpu_exact": {"known": False}}}

    def _input(self, name: str, *args: Any) -> dict[str, Any]:
        self.inputs.append((name, args))
        if name == self.fail_action:
            raise RuntimeError(f"{name} failed in fake service")
        return {"action": name, "args": list(args)}

    def capture(self, output: Path | None = None) -> FakeFrame:
        self.capture_paths.append(output)
        if self.fail_capture:
            raise OSError("fake frame capture unavailable")
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as stream:
                stream.write(FakeFrame.png)
        return FakeFrame()

    def tree(self, timeout: float = 60) -> Any:
        if self.fail_action == "tree":
            raise RuntimeError("native AX transport failed")
        self.tree_timeouts.append(timeout)
        if self.tree_delay:
            threading.Event().wait(self.tree_delay)
        value = self.trees[min(self.tree_calls, len(self.trees) - 1)]
        self.tree_calls += 1
        return value

    def screen(self, timeout: float = 60) -> dict:
        self.screen_timeouts.append(timeout)
        if self.fail_action == "screen":
            raise RuntimeError("native screen transport failed")
        return self.screen_info

    def launch(self, *args: Any) -> dict[str, Any]:
        return self._input("launch", *args)

    def tap(self, *args: Any) -> dict[str, Any]:
        return self._input("tap", *args)

    def tap_element(self, *args: Any) -> dict[str, Any]:
        return self._input("tap_element", *args)

    def swipe(self, *args: Any) -> dict[str, Any]:
        return self._input("swipe", *args)

    def text(self, *args: Any) -> dict[str, Any]:
        return self._input("text", *args)

    def button(self, *args: Any) -> dict[str, Any]:
        return self._input("button", *args)

    def fold(self, *args: Any) -> dict[str, Any]:
        return self._input("fold", *args)

    def terminate(self, *args: Any) -> dict[str, Any]:
        return self._input("terminate", *args)


class ScriptedTreeService(FakeService):
    def __init__(self, responses: list[Any]) -> None:
        super().__init__()
        self.responses = responses

    def tree(self, timeout: float = 60) -> Any:
        self.tree_calls += 1
        self.tree_timeouts.append(timeout)
        response = self.responses[min(self.tree_calls - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


def _write_scenario(tmp_path: Path, value: Any) -> Path:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(value, allow_nan=True), encoding="utf-8")
    return path


def test_run_scenario_reports_each_action_and_hashed_guest_screenshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scenarios.time, "sleep", lambda _seconds: None)
    document = {
        "version": 1,
        "steps": [
            {"action": "launch", "bundle_id": "com.example.Counter", "args": []},
            {"action": "tap", "x": 31.5, "y": 42},
            {"action": "tap_element", "identifier": "counterlab.increment"},
            {"action": "swipe", "x0": 10, "y0": 20, "x1": 50, "y1": 60, "duration_ms": 250},
            {"action": "text", "text": "hello"},
            {"action": "button", "name": "home"},
            {"action": "fold", "position": "open"},
            {"action": "terminate", "bundle_id": "com.example.Counter"},
            {"action": "screenshot", "name": "checkpoint"},
            {
                "action": "wait_text",
                "text": "1",
                "identifier": "counterlab.counter.value",
                "exact": True,
                "timeout": 1,
            },
            {"action": "assert_text", "text": "1", "identifier": "counterlab.counter.value", "exact": True},
            {"action": "wait", "seconds": 0},
        ],
    }
    path = _write_scenario(tmp_path, document)
    service = FakeService(
        trees=[
            [
                {"identifier": "status.clock", "label": "1"},
                {"identifier": "counterlab.counter.value", "label": "Count", "value": "0"},
            ],
            [
                {"identifier": "status.clock", "label": "1"},
                {"identifier": "counterlab.counter.value", "label": "Count", "value": "1"},
            ],
        ]
    )
    output = tmp_path / "runs" / "counter-001"

    report = run_scenario(service, path, output)

    assert report["status"] == "passed"
    assert report["graphics_policy"]["mode"] == "unknown"
    assert report["step_count"] == len(document["steps"])
    assert len(report["steps"]) == len(document["steps"])
    assert all(step["status"] == "passed" for step in report["steps"])
    assert all(step["duration_seconds"] >= 0 for step in report["steps"])
    assert all(step["result"] is not None for step in report["steps"])
    assert all(len(step["screenshots"]) == 1 for step in report["steps"])
    assert service.inputs == [
        ("launch", ("com.example.Counter", [])),
        ("tap", (31.5, 42.0)),
        ("tap_element", ("counterlab.increment",)),
        ("swipe", (10.0, 20.0, 50.0, 60.0, 250.0)),
        ("text", ("hello",)),
        ("button", ("home",)),
        ("fold", ("open",)),
        ("terminate", ("com.example.Counter",)),
    ]
    assert service.tree_calls == 3
    assert len(service.tree_timeouts) == 3
    assert report["steps"][9]["result"]["matches"] == ["1"]
    assert report["steps"][10]["result"]["matches"] == ["1"]
    assert (output / "screenshots" / "checkpoint.png").is_file()

    on_disk = json.loads((output / "report.json").read_text())
    assert on_disk == report
    for screenshot in report["screenshots"]:
        artifact = output / screenshot["path"]
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == screenshot["sha256"]
        assert screenshot["width"] == 200
        assert screenshot["height"] == 300
        assert screenshot["identity"] == "200x300:0"


@pytest.mark.parametrize("mode", ["compatible", "strict", "diagnostic", "unknown"])
def test_report_keeps_recorded_graphics_policy_on_action_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("LIMULATOR_GRAPHICS_MODE", "diagnostic" if mode == "strict" else "strict")
    policy = {"mode": mode, "reims_vgpu_exact": {"known": True, "set": True, "value": "all"}}
    service = FakeService(fail_action="tap")
    monkeypatch.setattr(service, "status", lambda: {"graphics_policy": policy})
    path = _write_scenario(tmp_path, {"version": 1, "steps": [{"action": "tap", "x": 1, "y": 2}]})

    with pytest.raises(ScenarioError) as error:
        run_scenario(service, path, tmp_path / "failed-run")

    report = json.loads(error.value.report_path.read_text())
    assert report["graphics_policy"] == policy
    assert report["status"] == "failed"


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (
            '{"version":1,"steps":[{"action":"launch","bundle_id":"com.example.App"},{"action":"tap","x":1}]}',
            "step 2 y",
        ),
        (
            '{"version":1,"steps":[{"action":"launch","bundle_id":"com.example.App","extra":true}]}',
            "unknown fields",
        ),
        (
            '{"version":1,"steps":[{"action":"launch","bundle_id":"com.example.App"},{"action":"shell","command":"id"}]}',
            "unknown action",
        ),
        (
            '{"version":1,"steps":[{"action":"launch","bundle_id":"com.example.App"},{"action":"tap","x":NaN,"y":2}]}',
            "non-finite",
        ),
        (
            '{"version":1,"steps":[{"action":"launch","bundle_id":"com.example.App","args":["--ignored"]}]}',
            "non-empty launch args are unsupported",
        ),
        (
            '{"version":1,"steps":[{"action":"tap_element"}]}',
            "step 1 identifier",
        ),
        (
            '{"version":1,"steps":[{"action":"assert_text","text":"1","exact":1}]}',
            "exact must be a boolean",
        ),
        (
            '{"version":1,"steps":[{"action":"launch","bundle_id":"com.example.App"},{"action":"wait","seconds":31}]}',
            "between 0 and 30",
        ),
        (
            '{"version":1,"steps":[{"action":"launch","bundle_id":"com.example.App"}],"extra":true}',
            "unknown scenario fields",
        ),
    ],
)
def test_all_steps_validate_before_first_input_and_write_failure_evidence(
    tmp_path: Path, contents: str, message: str
) -> None:
    path = tmp_path / "scenario.json"
    path.write_text(contents, encoding="utf-8")
    service = FakeService()
    output = tmp_path / "invalid-run"

    with pytest.raises(ScenarioError, match=message) as error:
        run_scenario(service, path, output)

    assert service.inputs == []
    assert error.value.report_path == output / "report.json"
    report = json.loads(error.value.report_path.read_text())
    assert report["status"] == "failed"
    assert report["steps"] == []
    assert report["failure_screenshots"][0]["path"] == "screenshots/failure.png"
    assert (output / "screenshots" / "failure.png").read_bytes() == FakeFrame.png


def test_action_failure_writes_failed_step_and_actual_failure_capture(tmp_path: Path) -> None:
    path = _write_scenario(tmp_path, {"version": 1, "steps": [{"action": "tap", "x": 1, "y": 2}]})
    service = FakeService(fail_action="tap")
    output = tmp_path / "failed-run"

    with pytest.raises(ScenarioError, match="tap failed in fake service") as error:
        run_scenario(service, path, output)

    report = json.loads(error.value.report_path.read_text())
    assert report["status"] == "failed"
    assert report["failure"]["step"] == 1
    assert report["steps"][0]["status"] == "failed"
    assert report["steps"][0]["result"] is None
    assert report["steps"][0]["screenshots"][0]["role"] == "failure"
    assert (
        hashlib.sha256((output / "screenshots" / "failure.png").read_bytes()).hexdigest()
        == report["steps"][0]["screenshots"][0]["sha256"]
    )


def test_failure_capture_error_is_reported_without_fabricated_screenshot(tmp_path: Path) -> None:
    path = _write_scenario(tmp_path, {"version": 1, "steps": [{"action": "tap", "x": 1, "y": 2}]})
    service = FakeService(fail_capture=True)
    output = tmp_path / "no-frame-run"

    with pytest.raises(ScenarioError, match="fake frame capture unavailable") as error:
        run_scenario(service, path, output)

    report = json.loads(error.value.report_path.read_text())
    assert report["status"] == "failed"
    assert "failure_screenshot_error" in report
    assert report["screenshots"] == []
    assert list((output / "screenshots").iterdir()) == []


def test_scenario_text_assertions_ignore_identifiers_and_metadata(tmp_path: Path) -> None:
    path = _write_scenario(
        tmp_path,
        {"version": 1, "steps": [{"action": "assert_text", "text": "Target Label"}]},
    )
    service = FakeService(trees=[{"identifier": "Target Label", "metadata": "Target Label"}])

    with pytest.raises(ScenarioError, match="expected visible text not found") as error:
        run_scenario(service, path, tmp_path / "not-visible")

    report = json.loads(error.value.report_path.read_text())
    assert report["steps"][0]["status"] == "failed"
    assert service.tree_calls == 1


def test_identifier_exact_text_does_not_match_clock_metadata_or_descendant(tmp_path: Path) -> None:
    path = _write_scenario(
        tmp_path,
        {
            "version": 1,
            "steps": [
                {
                    "action": "assert_text",
                    "text": "1",
                    "identifier": "counterlab.counter.value",
                    "exact": True,
                }
            ],
        },
    )
    service = FakeService(
        trees=[
            [
                {"identifier": "status.clock", "value": "1"},
                {
                    "identifier": "counterlab.counter.value",
                    "label": "Count",
                    "value": "10",
                    "metadata": "1",
                    "children": [{"identifier": "child.value", "value": "1"}],
                },
            ]
        ]
    )

    with pytest.raises(ScenarioError, match="exactly not found in AX element"):
        run_scenario(service, path, tmp_path / "wrong-element")


def test_identifier_text_defaults_to_case_insensitive_substring_matching(tmp_path: Path) -> None:
    path = _write_scenario(
        tmp_path,
        {
            "version": 1,
            "steps": [{"action": "assert_text", "text": "remember", "identifier": "counterlab.note"}],
        },
    )
    service = FakeService(trees=[[{"identifier": "counterlab.note", "value": "Remember this note"}]])

    report = run_scenario(service, path, tmp_path / "substring")

    assert report["steps"][0]["result"]["exact"] is False
    assert report["steps"][0]["result"]["matches"] == ["Remember this note"]


def test_wait_text_rejects_a_match_returned_after_its_deadline(tmp_path: Path) -> None:
    path = _write_scenario(
        tmp_path,
        {"version": 1, "steps": [{"action": "wait_text", "text": "Ready", "timeout": 0.01}]},
    )
    service = FakeService(trees=[[{"label": "Ready"}]], tree_delay=0.08)

    with pytest.raises(ScenarioError, match="timed out waiting for visible text") as error:
        run_scenario(service, path, tmp_path / "late-match")

    report = json.loads(error.value.report_path.read_text())
    assert report["steps"][0]["status"] == "failed"
    assert report["steps"][0]["duration_seconds"] >= 0.07
    assert service.tree_timeouts[0] <= 0.01
    assert service.tree_calls == 1


def _ax_readiness_error(
    message: str = "AX element query failed (-25216)",
    *,
    code: str = "failed",
    status_code: int = 400,
    details: bytes | None = None,
) -> ProtocolError:
    if details is None:
        details = json.dumps(
            {"error": {"code": code, "message": message}, "id": 1, "type": "response"}
        ).encode()
    return ProtocolError("HTTP error from vphoned", status_code=status_code, details=details)


@pytest.mark.parametrize("message", ["AX element query failed (-25215)", "AX element query failed (-25216)"])
def test_wait_text_retries_ax_readiness_error_until_text_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    monkeypatch.setattr(scenarios.time, "sleep", lambda _seconds: None)
    path = _write_scenario(
        tmp_path,
        {"version": 1, "steps": [{"action": "wait_text", "text": "Ready", "timeout": 1}]},
    )
    service = ScriptedTreeService(
        [_ax_readiness_error(message), [{"label": "Ready"}]],
    )

    report = run_scenario(service, path, tmp_path / "ax-ready")

    assert report["status"] == "passed"
    assert report["steps"][0]["result"]["matches"] == ["Ready"]
    assert service.tree_calls == 2
    assert len(service.tree_timeouts) == 2


def test_wait_text_bounds_persistent_ax_readiness_errors_by_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [10.0]
    monkeypatch.setattr(scenarios.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(scenarios.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    path = _write_scenario(
        tmp_path,
        {"version": 1, "steps": [{"action": "wait_text", "text": "Ready", "timeout": 0.51}]},
    )
    service = ScriptedTreeService([_ax_readiness_error()])

    with pytest.raises(
        ScenarioError, match="last AX readiness error: AX element query failed \\(-25216\\)"
    ) as error:
        run_scenario(service, path, tmp_path / "ax-unavailable")

    report = json.loads(error.value.report_path.read_text(encoding="utf-8"))
    assert report["failure"]["error"].endswith("last AX readiness error: AX element query failed (-25216)")
    assert report["screenshots"][0]["role"] == "failure"
    assert (error.value.report_path.parent / "screenshots" / "failure.png").is_file()
    assert service.tree_calls == 3
    assert all(0 < timeout <= 0.51 for timeout in service.tree_timeouts)
    assert now[0] == pytest.approx(10.51)


@pytest.mark.parametrize(
    ("status_code", "code", "message"),
    [
        (500, "failed", "AX element query failed (-25216)"),
        (400, "other", "AX element query failed (-25216)"),
        (400, "failed", "AX element query failed (-25217)"),
    ],
)
def test_wait_text_propagates_non_readiness_protocol_errors(
    tmp_path: Path, status_code: int, code: str, message: str
) -> None:
    path = _write_scenario(
        tmp_path,
        {"version": 1, "steps": [{"action": "wait_text", "text": "Ready", "timeout": 1}]},
    )
    protocol_error = _ax_readiness_error(message, code=code, status_code=status_code)
    service = ScriptedTreeService([protocol_error])

    with pytest.raises(ScenarioError) as error:
        run_scenario(service, path, tmp_path / "other-ax-error")

    report = json.loads(error.value.report_path.read_text(encoding="utf-8"))
    assert report["failure"]["error"] == str(protocol_error)
    assert report["failure"]["type"] == "ProtocolError"
    assert service.tree_calls == 1


@pytest.mark.parametrize(
    "details",
    [
        b"{invalid json",
        b'{"error":{"code":"failed","message":"AX element query failed (-25216)"}}' + b" " * 4096,
        b"[" * 1100 + b"]" * 1100,
    ],
)
def test_wait_text_does_not_retry_malformed_or_oversized_ax_errors(tmp_path: Path, details: bytes) -> None:
    path = _write_scenario(
        tmp_path,
        {"version": 1, "steps": [{"action": "wait_text", "text": "Ready", "timeout": 1}]},
    )
    protocol_error = _ax_readiness_error(details=details)
    service = ScriptedTreeService([protocol_error])

    with pytest.raises(ScenarioError) as error:
        run_scenario(service, path, tmp_path / "malformed-ax-error")

    report = json.loads(error.value.report_path.read_text(encoding="utf-8"))
    assert report["failure"]["error"] == str(protocol_error)
    assert service.tree_calls == 1


def test_tap_element_requires_and_dispatches_semantic_identifier(tmp_path: Path) -> None:
    path = _write_scenario(
        tmp_path,
        {"version": 1, "steps": [{"action": "tap_element", "identifier": "counterlab.reset"}]},
    )
    service = FakeService()

    report = run_scenario(service, path, tmp_path / "tap-element")

    assert service.inputs == [("tap_element", ("counterlab.reset",))]
    assert report["steps"][0]["status"] == "passed"


def _ax_target(*, identifier="target", x=50, y=75, visible=True):
    return {
        "source": "ax",
        "elements": [{"identifier": identifier, "visible": visible, "x": x, "y": y}],
        "truncated": False,
    }


@pytest.mark.parametrize("visible", [True, 1])
def test_scroll_accepts_native_ax_visibility_values(tmp_path, monkeypatch, visible):
    monkeypatch.setattr(scenarios.time, "sleep", lambda seconds: None)
    service = FakeService(trees=[_ax_target(visible=visible)])
    path = _write_scenario(
        tmp_path,
        {"version": 1, "steps": [{"action": "scroll_to_element", "identifier": "target"}]},
    )
    report = run_scenario(service, path, tmp_path / "native-visible")

    assert service.inputs == []
    assert report["steps"][0]["result"]["swipes"] == 0
    assert report["steps"][0]["result"]["activation_point"] == {
        "x": 50,
        "y": 75,
        "units": "points",
    }


@pytest.mark.parametrize("visible", [False, 0, 2, 1.0, "true", [], {}, None])
def test_scroll_rejects_noncanonical_ax_visibility_values(visible):
    target, reason = scenarios._scroll_target(_ax_target(visible=visible), "target", width=100, height=150)

    assert target is None
    assert reason == "target is not visible"


def test_scroll_reveals_offscreen_target_then_keeps_tap_semantic(tmp_path, monkeypatch):
    monkeypatch.setattr(scenarios.time, "sleep", lambda seconds: None)
    service = FakeService(trees=[_ax_target(visible=False), _ax_target()])
    path = _write_scenario(
        tmp_path,
        {
            "version": 1,
            "steps": [
                {"action": "scroll_to_element", "identifier": "target"},
                {"action": "tap_element", "identifier": "target"},
            ],
        },
    )
    report = run_scenario(service, path, tmp_path / "scroll-success")
    assert service.inputs == [
        ("swipe", (99.5, 233.22, 99.5, 95.68, 350, "200x300:0")),
        ("tap_element", ("target",)),
    ]
    result = report["steps"][0]["result"]
    assert result["swipes"] == 1
    assert result["activation_point"] == {"x": 50, "y": 75, "units": "points"}
    assert result["screen_points"] == {"width": 100, "height": 150}
    assert service.capture_paths[0] is None
    assert all(0 < timeout <= 30 for timeout in service.screen_timeouts + service.tree_timeouts)


def test_scroll_uses_guest_point_bounds_instead_of_framebuffer_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr(scenarios.time, "sleep", lambda seconds: None)
    service = FakeService(trees=[_ax_target(x=120), _ax_target()])
    path = _write_scenario(
        tmp_path,
        {
            "version": 1,
            "steps": [
                {"action": "scroll_to_element", "identifier": "target", "direction": "up"},
            ],
        },
    )
    report = run_scenario(service, path, tmp_path / "point-bounds")
    assert report["steps"][0]["result"]["swipes"] == 1
    assert service.inputs == [("swipe", (99.5, 95.68, 99.5, 233.22, 350, "200x300:0"))]


def test_scroll_absence_stops_at_swipe_limit_with_failure_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(scenarios.time, "sleep", lambda seconds: None)
    service = FakeService(trees=[_ax_target(identifier="another")])
    path = _write_scenario(
        tmp_path,
        {
            "version": 1,
            "steps": [
                {"action": "scroll_to_element", "identifier": "target", "max_swipes": 2},
            ],
        },
    )
    with pytest.raises(ScenarioError, match="exhausted 2 down swipes: target is not visible") as error:
        run_scenario(service, path, tmp_path / "absent")
    assert [name for name, args in service.inputs] == ["swipe", "swipe"]
    report = json.loads(error.value.report_path.read_text())
    assert report["failure"]["step"] == 1
    assert report["screenshots"][0]["role"] == "failure"


def test_scroll_recaptures_geometry_before_each_swipe(tmp_path, monkeypatch):
    monkeypatch.setattr(scenarios.time, "sleep", lambda seconds: None)

    class ResizedFrame(FakeFrame):
        width = 400
        height = 180
        identity = "400x180:0"

    class ResizingService(FakeService):
        def capture(self, output=None):
            frame = super().capture(output)
            return ResizedFrame() if self.capture_paths.count(None) >= 2 else frame

    service = ResizingService(trees=[_ax_target(visible=False), _ax_target(visible=False), _ax_target()])
    path = _write_scenario(
        tmp_path,
        {
            "version": 1,
            "steps": [
                {"action": "scroll_to_element", "identifier": "target"},
            ],
        },
    )
    report = run_scenario(service, path, tmp_path / "resized-scroll")
    first, second = [args for name, args in service.inputs if name == "swipe"]
    assert first[:5] == pytest.approx((99.5, 233.22, 99.5, 95.68, 350))
    assert second[:5] == pytest.approx((199.5, 139.62, 199.5, 57.28, 350))
    assert (first[-1], second[-1]) == ("200x300:0", "400x180:0")
    assert report["steps"][0]["result"]["swipes"] == 2


def test_scroll_does_not_swipe_after_a_capture_exceeds_the_deadline(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(scenarios.time, "monotonic", lambda: clock[0])

    class SlowCapture(FakeService):
        def capture(self, output=None):
            clock[0] += 0.2
            return super().capture(output)

    service = SlowCapture(trees=[_ax_target(visible=False)])
    path = _write_scenario(
        tmp_path,
        {
            "version": 1,
            "steps": [
                {"action": "scroll_to_element", "identifier": "target", "timeout": 0.1},
            ],
        },
    )
    with pytest.raises(ScenarioError, match="timed out after 0 down swipes"):
        run_scenario(service, path, tmp_path / "capture-budget")
    assert service.inputs == []


def test_scroll_never_accepts_a_late_match(tmp_path):
    service = FakeService(trees=[_ax_target()], tree_delay=0.05)
    path = _write_scenario(
        tmp_path,
        {
            "version": 1,
            "steps": [
                {"action": "scroll_to_element", "identifier": "target", "timeout": 0.01},
            ],
        },
    )
    with pytest.raises(ScenarioError, match="timed out after 0 down swipes"):
        run_scenario(service, path, tmp_path / "late-scroll")
    assert service.inputs == []


@pytest.mark.parametrize("failure", ["tree", "screen", "swipe"])
def test_scroll_propagates_non_target_failures_without_retry(tmp_path, failure):
    service = FakeService(trees=[_ax_target(identifier="another")], fail_action=failure)
    path = _write_scenario(
        tmp_path,
        {
            "version": 1,
            "steps": [
                {"action": "scroll_to_element", "identifier": "target"},
            ],
        },
    )
    expected = (
        "swipe failed in fake service"
        if failure == "swipe"
        else f"native {'AX' if failure == 'tree' else 'screen'} transport failed"
    )
    with pytest.raises(ScenarioError, match=expected):
        run_scenario(service, path, tmp_path / "rpc-error")
    assert len(service.inputs) <= 1


@pytest.mark.parametrize(
    "tree,message",
    [
        ({"error": "AX element query failed (-25215)"}, "AX element query failed"),
        ({"source": "ax", "elements": [], "truncated": True}, "was truncated"),
        (_ax_target(x=float("nan")), "invalid activation point"),
    ],
)
def test_scroll_refuses_invalid_accessibility_evidence(tmp_path, tree, message):
    service = FakeService(trees=[tree])
    path = _write_scenario(
        tmp_path,
        {
            "version": 1,
            "steps": [
                {"action": "scroll_to_element", "identifier": "target"},
            ],
        },
    )
    with pytest.raises(ScenarioError, match=message):
        run_scenario(service, path, tmp_path / "invalid-ax")
    assert service.inputs == []


@pytest.mark.parametrize(
    "fields",
    [
        {"max_swipes": True},
        {"max_swipes": -1},
        {"max_swipes": 13},
        {"direction": "left"},
        {"timeout": 0},
        {"timeout": 121},
        {"identifier": ""},
    ],
)
def test_scroll_inputs_validate_before_any_action(tmp_path, fields):
    path = _write_scenario(
        tmp_path,
        {
            "version": 1,
            "steps": [
                {"action": "launch", "bundle_id": "com.example.App"},
                {"action": "scroll_to_element", "identifier": "target", **fields},
            ],
        },
    )
    service = FakeService()
    with pytest.raises(ScenarioError):
        run_scenario(service, path, tmp_path / "invalid-scroll")
    assert service.inputs == []


def test_output_directory_is_stable_and_never_overwritten(tmp_path: Path) -> None:
    path = _write_scenario(tmp_path, {"version": 1, "steps": [{"action": "wait", "seconds": 0}]})
    output = tmp_path / "existing-run"
    output.mkdir()
    original = output / "preserve.txt"
    original.write_text("keep me", encoding="utf-8")
    service = FakeService()

    with pytest.raises(ScenarioError, match="cannot create scenario output directory"):
        run_scenario(service, path, output)

    assert original.read_text() == "keep me"
    assert service.inputs == []
