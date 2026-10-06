"""Fixed, credential-free guest command for the approved CSV recipe."""
from __future__ import annotations

import json
import os
import signal
import stat
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/recipe")
from cpu_recipes import describe_csv
from scientific_render import render_outputs

_OUTPUT_NAMES = ("summary.json", "summary.csv", "chart.svg", "report.md")
_MAX_INPUT = 1_048_576
_MAX_PARAMS = 16_384
_MAX_OUTPUT = 256 * 1024


def _read_fixed(path: Path, max_bytes: int) -> bytes:
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > max_bytes:
        raise ValueError("fixed compute input is not a bounded regular file")
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("fixed compute input exceeded its limit")
    return data


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON parameter")
        result[key] = value
    return result


def main() -> None:
    signal.alarm(30)
    data = _read_fixed(Path("/inputs/data.csv"), _MAX_INPUT)
    raw_params = _read_fixed(Path("/inputs/params.json"), _MAX_PARAMS)
    params = json.loads(raw_params.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(params, dict):
        raise ValueError("compute parameters must be a JSON object")
    summary = describe_csv(data, params)
    outputs = render_outputs(summary)
    if not isinstance(outputs, dict) or tuple(outputs) != _OUTPUT_NAMES:
        raise ValueError("compute renderer returned an unexpected file set or order")
    if any(not isinstance(outputs[name], bytes) for name in _OUTPUT_NAMES):
        raise ValueError("compute renderer must return output bytes")
    if sum(len(outputs[name]) for name in _OUTPUT_NAMES) > _MAX_OUTPUT:
        raise ValueError("compute outputs exceed 256 KiB")
    temporary = Path("/work/outputs.tmp")
    directory = Path("/work/outputs")
    marker = Path("/work/result-ready")
    if temporary.exists() or directory.exists() or marker.exists():
        raise ValueError("compute output paths must be unused")
    temporary.mkdir(mode=0o700, parents=False)
    for name in _OUTPUT_NAMES:
        path = temporary / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            view = memoryview(outputs[name])
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short compute output write")
                view = view[written:]
        finally:
            os.close(fd)
    os.rename(temporary, directory)
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
    try:
        os.write(fd, b"ready\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
