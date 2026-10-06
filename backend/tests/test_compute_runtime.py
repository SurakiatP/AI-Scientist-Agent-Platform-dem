from __future__ import annotations

import io
import importlib.util
import signal
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path as LocalPath
from types import ModuleType
from pathlib import Path
from uuid import uuid4

import pytest

from scientist.runtime_contracts import ComputeLaunchSpec
from scientist.supervisor import ExecutorRef
from scientist.compute_runtime import (
    create_compute,
    find_compute,
    poll_compute,
    read_compute_outputs,
    recipe_manifest_sha256,
    start_compute,
    stop_compute,
)


IMAGE = "sha256:" + "a" * 64
ENGINE_ID = "owned-engine-1"
CID = "b" * 64
OTHER_IMAGE = "sha256:" + "d" * 64
RECIPE_FILES = {
    "csv_describe.py": b"entrypoint",
    "cpu_recipes.py": b"recipe",
    "scientific_render.py": b"renderer",
}
OUTPUTS = {
    "summary.json": (b'{"rows":3}', 0o644),
    "summary.csv": (b"name,value\nrows,3\n", 0o644),
    "chart.svg": (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 140" role="img" aria-labelledby="title desc">'
        b'<title id="title">Means</title><desc id="desc">CSV means</desc>'
        b'<text x="12" y="54">x</text><rect x="440.000" y="40" width="150.000" height="22" fill="#3568a8"/>'
        b'</svg>',
        0o644,
    ),
    "report.md": (b"# Report\n", 0o644),
}


class Engine:
    def __init__(self, archive: bytes | None = None):
        self.archive = archive
        self.context = "colima-scientist-platform-test"
        self.commands: list[tuple[str, ...]] = []
        self.lookup_ids: list[str] = [CID]
        self.state = {"Running": False, "Status": "created", "ExitCode": 0}
        self.marker_ready = True
        self.container_image = IMAGE
        self.removed = False

    def engine_id(self) -> str:
        return ENGINE_ID

    def _verified_image_id(self, image: str, image_digest: str) -> str:
        assert image == image_digest == IMAGE
        return IMAGE

    def _docker(self, *args: str, input: bytes | None = None) -> str:
        self.commands.append(args)
        if args[0] == "create":
            return CID
        if args[:2] == ("inspect", "--format"):
            import json

            return f"{self.labels}|{CID}|{self.container_image}|{json.dumps(self.state)}"
        if args[0] == "ps":
            if "id=" in " ".join(args):
                return "" if self.removed else CID
            return "\n".join(self.lookup_ids)
        if args[0] in {"start", "stop", "rm"}:
            if args[0] == "start":
                self.state = {"Running": True, "Status": "running", "ExitCode": 0}
            elif args[0] in {"stop", "rm"}:
                self.state = {"Running": False, "Status": "exited", "ExitCode": 137}
                if args[0] == "rm":
                    self.removed = True
            return ""
        raise AssertionError(args)

    def set_labels(self, ref: ExecutorRef, image_digest: str = IMAGE) -> None:
        import json

        self.labels = json.dumps({
            "scientist.platform/run": str(ref.run_id),
            "scientist.platform/generation": str(ref.generation),
            "scientist.platform/executor": str(ref.executor_id),
            "scientist.platform/kind": "compute",
            "scientist.platform/incarnation": str(ref.process_incarnation),
            "scientist.platform/operation": str(ref.operation_id),
            "scientist.platform/image-digest": image_digest,
        })


@pytest.fixture(autouse=True)
def fake_owned_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    import scientist.compute_runtime as runtime

    monkeypatch.setattr(runtime, "_run_docker", lambda engine, *args, input=None: engine._docker(*args, input=input))
    monkeypatch.setattr(runtime, "_engine_identity", lambda engine: engine.engine_id())
    monkeypatch.setattr(
        runtime, "_accepted_image_id",
        lambda engine, digest: engine._verified_image_id(digest, digest),
    )
    monkeypatch.setattr(runtime, "_ready_marker", lambda engine, _ref: engine.marker_ready)


def make_spec(tmp_path: Path) -> ComputeLaunchSpec:
    recipe = tmp_path / "recipe"
    inputs = tmp_path / "inputs"
    recipe.mkdir()
    inputs.mkdir()
    for name, data in RECIPE_FILES.items():
        (recipe / name).write_bytes(data)
    (inputs / "data.csv").write_bytes(b"x,y\n1,2\n")
    (inputs / "params.json").write_bytes(b'{"numeric_columns":["x","y"]}')
    return ComputeLaunchSpec(
        profile_id="prof.csv-stdlib@py3.14.7",
        profile_version="1",
        image_digest=IMAGE,
        recipe_manifest_sha256=recipe_manifest_sha256(recipe),
        recipe_directory=recipe,
        input_directory=inputs,
        output_directory=tmp_path / "parent-staging",
    )


def make_ref() -> ExecutorRef:
    return ExecutorRef(
        executor_id=uuid4(), run_id=uuid4(), generation=4, kind="compute",
        operation_id="operation-1", process_incarnation=uuid4(),
        engine_id=ENGINE_ID, container_id=None,
    )


def make_tar(files: dict[str, tuple[bytes, int]] = OUTPUTS) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def test_create_only_creates_bound_compute_container(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    ref = make_ref()
    engine = Engine()
    engine.set_labels(ref)

    bound = create_compute(engine, ref, spec)

    assert bound.container_id == CID
    assert bound.engine_id == ENGINE_ID
    command = next(c for c in engine.commands if c[0] == "create")
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--read-only" in command and "--cap-drop" in command
    assert "/work" in " ".join(command)
    assert not any(c[0] == "start" for c in engine.commands)


def test_create_rejects_a_different_image_even_when_its_label_matches(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    ref = make_ref()
    engine = Engine()
    engine.container_image = OTHER_IMAGE
    engine.set_labels(ref, OTHER_IMAGE)

    with pytest.raises(RuntimeError, match="accepted image"):
        create_compute(engine, ref, spec)


def test_lost_create_ack_finds_the_exact_compute_identity() -> None:
    ref = make_ref()
    engine = Engine()
    engine.set_labels(ref)

    recovered = find_compute(engine, ref, expected_image_digest=IMAGE)

    assert recovered is not None
    assert recovered.container_id == CID
    assert recovered.operation_id == ref.operation_id


def test_bound_compute_lookup_inspects_cid_even_without_matching_labels() -> None:
    ref = replace(make_ref(), container_id=CID)
    engine = Engine()
    engine.set_labels(ref)
    engine.lookup_ids = []

    recovered = find_compute(engine, ref, expected_image_digest=IMAGE)

    assert recovered == ref
    assert any(call[0] == "ps" and "id=" in " ".join(call) for call in engine.commands)


def test_ambiguous_lost_create_ack_is_an_error() -> None:
    ref = make_ref()
    engine = Engine()
    engine.set_labels(ref)
    engine.lookup_ids = [CID, "c" * 64]

    with pytest.raises(RuntimeError, match="ambiguous"):
        find_compute(engine, ref, expected_image_digest=IMAGE)


def test_stale_generation_label_cannot_recover_a_compute_container() -> None:
    ref = make_ref()
    engine = Engine()
    engine.set_labels(ref)
    engine.labels = engine.labels.replace(str(ref.generation), str(ref.generation - 1))

    with pytest.raises(RuntimeError, match="labels differ"):
        find_compute(engine, ref, expected_image_digest=IMAGE)


@pytest.mark.parametrize("seam", ["find", "start", "stop", "poll", "outputs"])
def test_post_create_seams_reject_container_with_tampered_pinned_image(seam: str) -> None:
    ref = replace(make_ref(), container_id=CID)
    engine = Engine(make_tar())
    engine.set_labels(ref, OTHER_IMAGE)
    engine.container_image = OTHER_IMAGE
    engine.state = {"Running": False, "Status": "created", "ExitCode": 0}

    def invoke() -> object:
        if seam == "find":
            return find_compute(engine, ref, expected_image_digest=IMAGE)
        if seam == "start":
            return start_compute(engine, ref, expected_image_digest=IMAGE)
        if seam == "stop":
            return stop_compute(engine, ref, expected_image_digest=IMAGE)
        if seam == "poll":
            return poll_compute(engine, ref, expected_image_digest=IMAGE)
        return read_compute_outputs(engine, ref, expected_image_digest=IMAGE)

    with pytest.raises(RuntimeError, match="accepted image"):
        invoke()


def test_stop_during_execution_fences_and_removes_exact_container() -> None:
    ref = replace(make_ref(), container_id=CID)
    engine = Engine()
    engine.set_labels(ref)
    engine.state = {"Running": True, "Status": "running", "ExitCode": 0}

    stop_compute(engine, ref, expected_image_digest=IMAGE)

    assert [call[0] for call in engine.commands if call[0] in {"stop", "rm"}] == ["stop", "rm"]


def test_wall_timeout_stops_container_and_reports_timeout() -> None:
    ref = replace(make_ref(), container_id=CID)
    engine = Engine()
    engine.set_labels(ref)
    engine.marker_ready = False
    engine.state = {
        "Running": True, "Status": "running", "ExitCode": 0,
        "StartedAt": "2000-01-01T00:00:00Z",
    }

    assert poll_compute(engine, ref, expected_image_digest=IMAGE) == 124
    assert "stop" in [call[0] for call in engine.commands]


def test_wall_timeout_still_fences_a_guest_with_a_ready_marker() -> None:
    ref = replace(make_ref(), container_id=CID)
    engine = Engine()
    engine.set_labels(ref)
    engine.state = {
        "Running": True, "Status": "running", "ExitCode": 0,
        "StartedAt": "2000-01-01T00:00:00Z",
    }

    assert poll_compute(engine, ref, expected_image_digest=IMAGE) == 124
    assert "stop" in [call[0] for call in engine.commands]


def test_output_archive_accepts_only_the_four_safe_regular_files(monkeypatch: pytest.MonkeyPatch) -> None:
    import scientist.compute_runtime as runtime

    engine = Engine(make_tar())
    ref = make_ref()
    ref = replace(ref, container_id=CID)
    engine.set_labels(ref)
    engine.state = {"Running": True, "Status": "running", "ExitCode": 0}
    monkeypatch.setattr(runtime, "_container_archive", lambda _engine, _cid: make_tar())

    outputs = read_compute_outputs(engine, ref, expected_image_digest=IMAGE)

    assert tuple(outputs) == tuple(OUTPUTS)
    assert outputs["summary.json"] == OUTPUTS["summary.json"][0]


def test_guest_entrypoint_reads_fixed_inputs_and_writes_only_four_outputs(
    tmp_path: LocalPath, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = tmp_path / "inputs"
    work = tmp_path / "work"
    inputs.mkdir()
    work.mkdir()
    (inputs / "data.csv").write_bytes(b"x,y\n1,2\n")
    (inputs / "params.json").write_bytes(b'{"numeric_columns":["x","y"]}')

    class MappedPath:
        def __new__(cls, value: str):
            if value == "/inputs/data.csv":
                value = str(inputs / "data.csv")
            elif value == "/inputs/params.json":
                value = str(inputs / "params.json")
            elif value == "/work/outputs":
                value = str(work / "outputs")
            elif value == "/work/outputs.tmp":
                value = str(work / "outputs.tmp")
            elif value == "/work/result-ready":
                value = str(work / "result-ready")
            return LocalPath(value)

    alarms: list[int] = []
    alarm_events: list[tuple[object, ...]] = []

    def describe_csv(data: bytes, params: dict[str, object]) -> dict[str, object]:
        assert alarms == [30]
        return {"data": data.decode(), "params": params}

    cpu = ModuleType("cpu_recipes")
    cpu.describe_csv = describe_csv
    renderer = ModuleType("scientific_render")
    renderer.render_outputs = lambda _summary: {
        name: content for name, (content, _mode) in OUTPUTS.items()
    }
    monkeypatch.setitem(sys.modules, "cpu_recipes", cpu)
    monkeypatch.setitem(sys.modules, "scientific_render", renderer)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: alarm_events.append(("signal", signum, handler)))
    monkeypatch.setattr(
        signal, "alarm",
        lambda seconds: (alarms.append(seconds), alarm_events.append(("alarm", seconds)), 0)[-1],
    )
    entrypoint = LocalPath(__file__).resolve().parents[2] / "runtime/compute_entrypoint.py"
    spec = importlib.util.spec_from_file_location("compute_entrypoint_test", entrypoint)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr("pathlib.Path", MappedPath)
    class GuestReady(Exception):
        pass

    paused: list[bool] = []

    def guest_pause() -> None:
        paused.append(True)
        raise GuestReady

    monkeypatch.setattr(signal, "pause", guest_pause)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "Path", MappedPath)

    with pytest.raises(GuestReady):
        module.main()

    assert {path.name for path in (work / "outputs").iterdir()} == set(OUTPUTS)
    assert (work / "result-ready").read_bytes() == b"ready\n"
    assert alarms == [30]
    assert alarm_events[0][0] == "signal", "guest must install its fatal alarm handler before arming the timer"
    assert alarm_events[0][1] == signal.SIGALRM
    assert callable(alarm_events[0][2])
    assert alarm_events[1:] == [("alarm", 30)]
    assert paused == [True]
    assert not (tmp_path / "parent-staging").exists()


def test_guest_alarm_handler_exits_142_in_a_controlled_child() -> None:
    entrypoint = LocalPath(__file__).resolve().parents[2] / "runtime/compute_entrypoint.py"
    child = f"""
import importlib.util, signal, sys, types
sys.modules["cpu_recipes"] = types.ModuleType("cpu_recipes")
sys.modules["cpu_recipes"].describe_csv = lambda *_: None
sys.modules["scientific_render"] = types.ModuleType("scientific_render")
sys.modules["scientific_render"].render_outputs = lambda *_: None
spec = importlib.util.spec_from_file_location("alarm_entrypoint", {str(entrypoint)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
signal.signal(signal.SIGALRM, module._exit_on_alarm)
signal.alarm(1)
signal.pause()
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", child],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=3,
    )
    assert result.returncode == 142, result.stderr.decode("utf-8", "replace")
