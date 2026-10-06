"""Fail-closed worker bootstrap for the pinned Hermes runtime."""

from __future__ import annotations

import json
import os
import stat
import sys
import sysconfig
import time
from pathlib import Path
from typing import Any

from scientist.runtime_contracts import (
    BootstrapMetadata,
    RuntimeContextV1,
    WorkspaceFile,
    validate_workspace_path,
)
from scientist.instruction_loader import InstructionLoadError, load_instruction_pins, validate_scientific_binding
from scientist.runtime_adapter import (
    BudgetExhausted,
    RuntimeAdapter,
    RuntimeAdapterError,
    build_native_agent,
    install_saved_turn_continuation,
    _compacted_context,
)

_BOOTSTRAP = Path("/run/scientist/bootstrap")
_WORKSPACE = Path("/workspace")
_READY = Path("/run/scientist/readiness/ready")
_SCIENTIFIC_REGISTRY = Path("/opt/scientist/runtime/capability-registry.json")
_SCIENTIFIC_MANIFEST = Path("/opt/scientist/runtime/skills-manifest.json")
_SCIENTIFIC_BUNDLE_ROOT = Path("/opt/scientist")
_MAX_JSON_BYTES = 90 * 1024 * 1024
_MAX_TOKEN_BYTES = 4096


class BootstrapError(RuntimeError):
    """Invalid, incomplete, or unsafe worker bootstrap data."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapError("duplicate bootstrap JSON key")
        result[key] = value
    return result


def _read_regular(path: Path, maximum: int) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > maximum:
            raise BootstrapError("bootstrap file is unsafe or oversized")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            chunks = bytearray()
            while len(chunks) <= maximum:
                block = os.read(fd, min(65536, maximum + 1 - len(chunks)))
                if not block:
                    break
                chunks.extend(block)
        finally:
            os.close(fd)
    except OSError as exc:
        raise BootstrapError("bootstrap file unavailable") from exc
    if len(chunks) > maximum or len(chunks) != info.st_size:
        raise BootstrapError("bootstrap file changed or exceeds its limit")
    return bytes(chunks)


def _read_json(path: Path, maximum: int) -> Any:
    try:
        return json.loads(_read_regular(path, maximum), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("invalid bootstrap JSON") from exc


def wait_until_ready(path: Path = _READY, *, timeout_seconds: float = 60.0) -> None:
    """Wait for the supervisor's literal readiness marker before Hermes import."""
    if not 0 < timeout_seconds <= 300:
        raise ValueError("readiness timeout must be bounded")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if _read_regular(path, 16) == b"ready":
                return
        except BootstrapError:
            pass
        time.sleep(0.05)
    raise BootstrapError("supervisor readiness marker timed out")


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise BootstrapError("private runtime directory unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise BootstrapError("private runtime path is not a directory")


def _sanitize_environment() -> tuple[str, str, str, str]:
    required = {
        "SCIENTIST_BOOTSTRAP_DIR": os.environ.get("SCIENTIST_BOOTSTRAP_DIR", str(_BOOTSTRAP)),
        "SCIENTIST_READINESS_FILE": os.environ.get("SCIENTIST_READINESS_FILE", str(_READY)),
        "SCIENTIST_WORKSPACE": os.environ.get("SCIENTIST_WORKSPACE", str(_WORKSPACE)),
        "SCIENTIST_BROKER_URL": os.environ.get("SCIENTIST_BROKER_URL", ""),
        "SCIENTIST_CAPABILITY_FILE": os.environ.get(
            "SCIENTIST_CAPABILITY_FILE", "/run/scientist/capability/token"
        ),
    }
    if any("\x00" in value for value in required.values()) or not required["SCIENTIST_BROKER_URL"]:
        raise BootstrapError("required worker configuration missing")
    os.environ.clear()
    os.environ.update(
        {
            **required,
            "HOME": "/home/scientist",
            "HERMES_HOME": "/run/hermes-home",
            "XDG_CONFIG_HOME": "/run/hermes-home/config",
            "XDG_DATA_HOME": "/run/hermes-home/data",
            "XDG_CACHE_HOME": "/run/hermes-home/cache",
            "TMPDIR": "/tmp",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    for directory in (Path("/home/scientist"), Path("/run/hermes-home")):
        _private_directory(directory)
    for directory in ("config", "data", "cache"):
        _private_directory(Path("/run/hermes-home") / directory)
    return (
        required["SCIENTIST_BOOTSTRAP_DIR"],
        required["SCIENTIST_WORKSPACE"],
        required["SCIENTIST_BROKER_URL"],
        required["SCIENTIST_CAPABILITY_FILE"],
    )


def _pin_import_paths() -> None:
    """Retain stdlib/site-packages, remove cwd/workspace, pin platform sources."""
    workspace = os.path.realpath("/workspace")
    allowed = {
        os.path.realpath(value)
        for key, value in sysconfig.get_paths().items()
        if key in {"stdlib", "platstdlib", "purelib", "platlib"}
    }
    retained = sorted(
        path
        for path in allowed
        if path != workspace and not path.startswith(workspace + os.sep) and Path(path).is_dir()
    )
    sys.path[:] = ["/opt/scientist", "/opt/hermes", *retained]


def _materialize_workspace(root: Path, values: Any) -> None:
    if not isinstance(values, list) or len(values) > 1024:
        raise BootstrapError("invalid workspace manifest")
    root.mkdir(mode=0o777, parents=True, exist_ok=True)
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise BootstrapError("workspace root is unsafe")
    if any(root.iterdir()):
        raise BootstrapError("workspace must be empty before materialization")
    total = 0
    seen: set[str] = set()
    for raw in values:
        try:
            entry = WorkspaceFile.model_validate(raw)
            relative = validate_workspace_path(entry.path)
            if relative in seen:
                raise BootstrapError("duplicate workspace path")
            seen.add(relative)
            content = entry.decoded_data()
        except (ValueError, TypeError) as exc:
            raise BootstrapError("invalid workspace entry") from exc
        total += len(content)
        if total > 64 * 1024 * 1024:
            raise BootstrapError("workspace exceeds its byte limit")
        target = root / relative
        current = root
        for part in Path(relative).parts[:-1]:
            current = current / part
            current.mkdir(mode=0o700, exist_ok=True)
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise BootstrapError("workspace parent path is unsafe")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise BootstrapError("workspace write failed")
                view = view[written:]
        finally:
            os.close(fd)


def load_bootstrap(bootstrap_dir: Path) -> tuple[RuntimeContextV1, list[dict[str, Any]], BootstrapMetadata]:
    context = RuntimeContextV1.model_validate(
        _read_json(bootstrap_dir / "context.json", 1024 * 1024)
    )
    workspace = _read_json(bootstrap_dir / "workspace.json", _MAX_JSON_BYTES)
    metadata = BootstrapMetadata.model_validate(_read_json(bootstrap_dir / "metadata.json", 4096))
    if not isinstance(workspace, list):
        raise BootstrapError("workspace bootstrap must be a list")
    return context, workspace, metadata


def _load_scientific_bundle(context: RuntimeContextV1):
    """Revalidate optional scientific authority against immutable worker files."""
    binding = context.plan.scientific
    if binding is None:
        return None
    try:
        pins = load_instruction_pins(_SCIENTIFIC_MANIFEST)
        return validate_scientific_binding(
            binding,
            registry_path=_SCIENTIFIC_REGISTRY,
            bundle_root=_SCIENTIFIC_BUNDLE_ROOT,
            pinned_hashes=pins,
            expected_image_digest=context.image_digest,
        )
    except (InstructionLoadError, OSError, ValueError, TypeError) as exc:
        raise BootstrapError("scientific authority failed worker bootstrap validation") from exc


def _paused_for_budget(adapter: RuntimeAdapter) -> bool:
    """The broker recorded the owner budget wait and nothing else is left in flight.

    The flag is set only by the broker's budget_exhausted 409 (no journal row, no provider
    call), and every later dispatch fails before I/O. Every other effect of this generation
    must be committed, and no native tool batch may be half-applied (pending_assistant is
    cleared only once the whole batch is checkpointed), so recovery loses nothing.
    """
    return (adapter.budget_exhausted and not adapter.unresolved_effects
            and adapter.context.pending_assistant is None)


def _caused_by_budget_wait(exc: BaseException | None) -> bool:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, BudgetExhausted):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def run_worker() -> None:
    wait_until_ready(Path(os.environ.get("SCIENTIST_READINESS_FILE", str(_READY))))
    bootstrap_dir, workspace_name, broker_url, capability_name = _sanitize_environment()
    context, workspace, metadata = load_bootstrap(Path(bootstrap_dir))
    scientific_bundle = _load_scientific_bundle(context)
    capability = _read_regular(Path(capability_name), _MAX_TOKEN_BYTES).decode("utf-8")
    if not capability or "\n" in capability or "\r" in capability:
        raise BootstrapError("invalid worker capability")
    workspace_dir = Path(workspace_name)
    _materialize_workspace(workspace_dir, workspace)
    _pin_import_paths()

    adapter = RuntimeAdapter(
        context,
        broker_url=broker_url,
        capability=capability,
        workspace_dir=workspace_dir,
        checkpoint_revision=metadata.checkpoint_revision,
    )
    if scientific_bundle is not None:
        adapter.scientific_instruction_bundle = scientific_bundle
    try:
        agent = build_native_agent(adapter, workspace_dir=workspace_dir)
        # Imports and private Hermes initialization finish before entering the
        # writable work tree; /workspace is never a Python import path.
        os.chdir(workspace_dir)
        conversation_history = adapter.native_history()
        pending = adapter.context.pending_assistant
        if pending is not None:
            from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageToolCall

            raw_message = conversation_history[pending.message_index]
            raw_calls = raw_message.get("tool_calls") or []
            if not raw_calls or pending.next_tool_index >= len(raw_calls):
                raise RuntimeAdapterError("pending tool cursor is outside the saved assistant batch")
            applied_by_raw = {item.raw_id: item.applied_id for item in pending.applied_tool_ids}
            mapped_calls = []
            for raw_call in raw_calls[pending.next_tool_index :]:
                raw_id = raw_call.get("id")
                call = raw_call.get("function") or {}
                applied_id = applied_by_raw.get(raw_id, raw_id)
                mapped_calls.append(
                    ChatCompletionMessageToolCall(
                        id=applied_id,
                        type="function",
                        function={"name": call.get("name"), "arguments": call.get("arguments")},
                    )
                )
            assistant_message = ChatCompletionMessage(
                role="assistant",
                content=raw_message.get("content"),
                tool_calls=mapped_calls,
            )
            agent._execute_tool_calls(
                assistant_message,
                conversation_history,
                str(context.run_id),
                0,
            )
        conversation_history = adapter.native_history()
        install_saved_turn_continuation(agent, adapter)
        saved_user = conversation_history[adapter.context.current_turn_user_index]
        result = agent.run_conversation(
            user_message=saved_user["content"],
            system_message=context.system_prompt,
            conversation_history=conversation_history,
            task_id=str(context.run_id),
        )
        if (
            not isinstance(result, dict)
            or result.get("completed") is not True
            or result.get("failed")
            or result.get("interrupted")
            or result.get("partial")
            or result.get("error")
            or adapter.context.pending_assistant is not None
        ):
            # Pinned Hermes swallows the transport error and returns a failed turn dict,
            # so the BudgetExhausted chain never reaches us; the adapter state is the proof.
            if isinstance(result, dict) and result.get("completed") is not True and _paused_for_budget(adapter):
                print("worker paused for owner budget decision (failed Hermes turn)", file=sys.stderr)
                return
            raise RuntimeAdapterError("Hermes did not return a complete, quiescent turn")
        final_messages = result.get("messages")
        final_user_index = result.get("current_turn_user_idx")
        if (
            not isinstance(final_messages, list)
            or result.get("turn_id") != str(adapter.context.turn_id)
            or type(final_user_index) is not int
            or final_user_index < 0
            or final_user_index >= len(final_messages)
            or not isinstance(final_messages[final_user_index], dict)
            or final_messages[final_user_index].get("role") != "user"
        ):
            raise RuntimeAdapterError("Hermes final turn omitted its canonical transcript")
        adapter.sync_primary_history(
            final_messages,
            current_turn_user_index=final_user_index,
            native_turn_timestamp=getattr(agent, "_current_turn_timestamp", None),
        )
        compressor = getattr(agent, "context_compressor", None)
        if compressor is not None:
            adapter.context = adapter.context.model_copy(
                update={
                    "compacted_context": _compacted_context(compressor),
                    "system_prompt": getattr(agent, "_cached_system_prompt", context.system_prompt)
                    or context.system_prompt,
                }
            )
        adapter.context = adapter.context.model_copy(update={"boundary": "final"})
        adapter._checkpoint("final")
    except Exception as exc:
        # Exit cleanly (code 0) only when the broker recorded the owner budget wait (adapter
        # flag) and this failure is that wait, possibly wrapped by the SDK/Hermes. Recovery
        # keeps the wait; anything else is still a worker failure.
        if not (_paused_for_budget(adapter) and _caused_by_budget_wait(exc)):
            raise
        print(f"worker paused for owner budget decision ({type(exc).__name__})", file=sys.stderr)
    finally:
        adapter.close()


def main() -> None:
    run_worker()


if __name__ == "__main__":
    main()
