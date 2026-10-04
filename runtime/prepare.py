"""Bounded, non-executing local preparation for safe text formats."""

from __future__ import annotations

import csv
import json
import stat
import zipfile
from io import StringIO
from pathlib import Path

MAX_BYTES = 25 * 1024 * 1024
MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 256
MAX_JSON_DEPTH = 64
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_EXPANDED = 100 * 1024 * 1024


class PreparationError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def prepare_local_file(path: Path, output_dir: Path) -> dict:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise PreparationError("invalid_path")
    if path.stat().st_size > MAX_BYTES:
        raise PreparationError("file_too_large")
    suffix = path.suffix.lower()
    if suffix not in {".pdf", ".csv", ".xlsx", ".json", ".txt", ".md"}:
        raise PreparationError("unsupported_file_type")
    raw = path.read_bytes()
    if suffix != ".pdf" and raw.startswith(b"%PDF-"):
        raise PreparationError("type_mismatch")
    if suffix != ".xlsx" and raw.startswith(b"PK\x03\x04"):
        raise PreparationError("type_mismatch")
    if suffix == ".pdf":
        if not raw.startswith(b"%PDF-"):
            raise PreparationError("type_mismatch")
        if b"%%EOF" not in raw[-2048:]:
            raise PreparationError("invalid_pdf")
        raise PreparationError("sandbox_required")
    if suffix == ".xlsx":
        _inspect_xlsx(raw)
        # Host-side PDF/XLSX parser execution stays disabled until B5 provides isolation.
        raise PreparationError("sandbox_required")
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise PreparationError("invalid_encoding") from exc
    if suffix == ".json":
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise PreparationError("invalid_json") from exc
        _check_depth(value)
        extracted = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    elif suffix == ".csv":
        rows = []
        try:
            for index, row in enumerate(csv.reader(StringIO(text, newline=""), strict=True)):
                if index >= MAX_ROWS:
                    raise PreparationError("row_limit")
                if len(row) > MAX_COLUMNS:
                    raise PreparationError("column_limit")
                rows.append(row)
        except csv.Error as exc:
            raise PreparationError("invalid_csv") from exc
        extracted = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    else:
        extracted = text
    encoded = extracted.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise PreparationError("extracted_text_limit")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "extracted.txt"
    try:
        with target.open("xb") as output:
            output.write(encoded)
    except FileExistsError as exc:
        raise PreparationError("output_exists") from exc
    return {"filename": path.name, "source_size": len(raw), "extracted_size": len(encoded), "path": str(target)}


def _check_depth(root: object) -> None:
    stack = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise PreparationError("json_depth")
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)


def _inspect_xlsx(raw: bytes) -> None:
    try:
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS or sum(item.file_size for item in members) > MAX_ZIP_EXPANDED:
                raise PreparationError("archive_limit")
            for item in members:
                name = item.filename.replace("\\", "/")
                mode = item.external_attr >> 16
                if name.startswith("/") or ".." in name.split("/") or stat.S_ISLNK(mode):
                    raise PreparationError("unsafe_archive_path")
                if name.lower().startswith(("xl/externallinks/",)) or name.lower().endswith("vbaproject.bin"):
                    raise PreparationError("active_content")
            if "[Content_Types].xml" not in archive.namelist():
                raise PreparationError("invalid_xlsx")
    except PreparationError:
        raise
    except (zipfile.BadZipFile, OSError) as exc:
        raise PreparationError("invalid_xlsx") from exc
