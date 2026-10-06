"""Bounded, descriptive CSV statistics for the S2 CPU recipe."""

from __future__ import annotations

import csv
import hashlib
import io
import math
import statistics
import threading
import unicodedata

MAX_CSV_BYTES = 1_048_576
MAX_ROWS = 10_000
MAX_COLUMNS = 8
MAX_HEADER_CHARS = 128
_CSV_LOCK = threading.Lock()


def describe_csv(data: bytes, params: dict[str, object]) -> dict[str, object]:
    if not isinstance(data, bytes) or len(data) > MAX_CSV_BYTES:
        raise ValueError("CSV input exceeds its byte limit")
    if type(params) is not dict or set(params) != {"numeric_columns"}:
        raise ValueError("recipe parameters must contain only numeric_columns")
    selected = params["numeric_columns"]
    if type(selected) is not list or not 1 <= len(selected) <= MAX_COLUMNS:
        raise ValueError("numeric_columns must select one to eight headers")
    if any(not isinstance(name, str) or not name for name in selected) or len(set(selected)) != len(selected):
        raise ValueError("numeric_columns must be unique nonempty header names")
    try:
        source = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV input must be valid UTF-8") from exc

    # The parser's global field limit is protected while temporarily raised to the whole input bound.
    with _CSV_LOCK:
        old_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(MAX_CSV_BYTES)
            rows = list(csv.reader(io.StringIO(source, newline=""), strict=True))
        except (csv.Error, UnicodeError) as exc:
            raise ValueError("CSV input is malformed") from exc
        finally:
            csv.field_size_limit(old_limit)
    if not rows:
        raise ValueError("CSV must contain a header row")
    headers = rows[0]
    if not 1 <= len(headers) <= MAX_COLUMNS or any(
        not header or len(header) > MAX_HEADER_CHARS or _has_controls(header) or not _xml10_legal(header)
        for header in headers
    ) or len(set(headers)) != len(headers):
        raise ValueError("CSV headers must be unique, nonempty, bounded, and control-free")
    if len(rows) - 1 > MAX_ROWS:
        raise ValueError("CSV row limit exceeded")
    if any(name not in headers for name in selected):
        raise ValueError("numeric_columns contains an unknown header")
    if any(len(row) != len(headers) for row in rows[1:]):
        raise ValueError("CSV rows must match header width")
    if any(_has_controls(cell) for row in rows[1:] for cell in row):
        raise ValueError("CSV cells must not contain control characters")

    columns: list[dict[str, object]] = []
    for name in selected:
        values: list[float] = []
        missing = 0
        for row in rows[1:]:
            cell = row[headers.index(name)]
            if cell == "":
                missing += 1
                continue
            try:
                value = float(cell)
            except (ValueError, OverflowError) as exc:
                raise ValueError(f"column {name!r} must contain only finite numbers or empty cells") from exc
            if not math.isfinite(value):
                raise ValueError(f"column {name!r} contains a nonfinite number")
            values.append(value)
        if values:
            try:
                mean = statistics.fmean(values)
                median = statistics.median(values)
                sample_sd = statistics.stdev(values) if len(values) >= 2 else None
            except (ArithmeticError, OverflowError, ValueError) as exc:
                raise ValueError(f"column {name!r} statistics overflowed") from exc
            if not all(math.isfinite(value) for value in (mean, median)) or (
                sample_sd is not None and not math.isfinite(sample_sd)
            ):
                raise ValueError(f"column {name!r} statistics are nonfinite")
            minimum: float | None = min(values)
            maximum: float | None = max(values)
        else:
            minimum = maximum = mean = median = sample_sd = None
        columns.append({
            "name": name,
            "valid": len(values),
            "missing": missing,
            "min": minimum,
            "max": maximum,
            "mean": mean,
            "median": median,
            "sample_sd": sample_sd,
        })
    return {
        "schema_version": 1,
        "input_sha256": hashlib.sha256(data).hexdigest(),
        "rows": len(rows) - 1,
        "columns": columns,
    }


def _has_controls(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)


def _xml10_legal(value: str) -> bool:
    return all(
        0x20 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF
        for char in value
    )
