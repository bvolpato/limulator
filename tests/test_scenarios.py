from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from limulator import scenarios
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
    ) -> None:
        self.inputs: list[tuple[str, tuple[Any, ...]]] = []
        self.capture_paths: list[Path | None] = []
        self.trees = trees or [{"label": "Counter"}]
        self.tree_calls = 0
        self.tree_timeouts: list[float] = []
        self.tree_delay = tree_delay
        self.fail_action = fail_action
        self.fail_capture = fail_capture

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
        self.tree_timeouts.append(timeout)
        if self.tree_delay:
            threading.Event().wait(self.tree_delay)
        value = self.trees[min(self.tree_calls, len(self.trees) - 1)]
        self.tree_calls += 1
        return value

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


def test_tap_element_requires_and_dispatches_semantic_identifier(tmp_path: Path) -> None:
    path = _write_scenario(
        tmp_path,
        {"version": 1, "steps": [{"action": "tap_element", "identifier": "counterlab.reset"}]},
    )
    service = FakeService()

    report = run_scenario(service, path, tmp_path / "tap-element")

    assert service.inputs == [("tap_element", ("counterlab.reset",))]
    assert report["steps"][0]["status"] == "passed"


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
