"""Deterministic static outputs for S2 descriptive statistics."""

from __future__ import annotations

import csv
import html
import io
import json
import math
import re
import unicodedata

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COLUMN_KEYS = ("name", "valid", "missing", "min", "max", "mean", "median", "sample_sd")


def render_outputs(summary: dict[str, object]) -> dict[str, bytes]:
    columns = _validate_summary(summary)
    json_bytes = (json.dumps(summary, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")

    csv_stream = io.StringIO(newline="")
    writer = csv.writer(csv_stream, lineterminator="\n")
    writer.writerow(_COLUMN_KEYS)
    for column in columns:
        writer.writerow([_safe_csv_label(column["name"])] + [column[key] for key in _COLUMN_KEYS[1:]])

    svg = _render_svg(columns)
    report = _render_report(summary, columns)
    outputs = {
        "summary.json": json_bytes,
        "summary.csv": csv_stream.getvalue().encode("utf-8"),
        "chart.svg": svg.encode("utf-8"),
        "report.md": report.encode("utf-8"),
    }
    if sum(map(len, outputs.values())) > 256 * 1024:
        raise ValueError("rendered outputs exceed the total byte limit")
    return outputs


def _validate_summary(summary: dict[str, object]) -> list[dict[str, object]]:
    if type(summary) is not dict or set(summary) != {"schema_version", "input_sha256", "rows", "columns"}:
        raise ValueError("summary shape is invalid")
    if type(summary["schema_version"]) is not int or summary["schema_version"] != 1 or type(summary["rows"]) is not int or summary["rows"] < 0:
        raise ValueError("summary metadata is invalid")
    if not isinstance(summary["input_sha256"], str) or not _SHA256.fullmatch(summary["input_sha256"]):
        raise ValueError("summary input hash is invalid")
    columns = summary["columns"]
    if type(columns) is not list or not 1 <= len(columns) <= 8:
        raise ValueError("summary columns are invalid")
    names: set[str] = set()
    for column in columns:
        if type(column) is not dict or tuple(column) != _COLUMN_KEYS:
            raise ValueError("summary column shape is invalid")
        name = column["name"]
        if not isinstance(name, str) or not name or len(name) > 128 or _has_controls(name) or not _xml10_legal(name) or name in names:
            raise ValueError("summary label is invalid")
        names.add(name)
        valid, missing = column["valid"], column["missing"]
        if type(valid) is not int or type(missing) is not int or min(valid, missing) < 0 or valid + missing != summary["rows"]:
            raise ValueError("summary counts are invalid")
        numeric = (column["min"], column["max"], column["mean"], column["median"])
        deviation = column["sample_sd"]
        if valid == 0:
            if any(value is not None for value in (*numeric, deviation)):
                raise ValueError("empty summary statistics must be null")
        elif any(type(value) not in (int, float) or not math.isfinite(value) for value in numeric):
            raise ValueError("summary statistics must be finite numbers")
        if (valid < 2 and deviation is not None) or (
            valid >= 2 and (type(deviation) not in (int, float) or not math.isfinite(deviation) or deviation < 0)
        ):
            raise ValueError("sample deviation is invalid")
    return columns


def _render_svg(columns: list[dict[str, object]]) -> str:
    means = [column["mean"] for column in columns if column["mean"] is not None]
    scale = max((abs(value) for value in means), default=0.0)
    height = 56 + 42 * len(columns)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 {height}" role="img" aria-labelledby="title desc">',
        "<title id=\"title\">Descriptive means</title>",
        "<desc id=\"desc\">Mean values for selected CSV columns</desc>",
    ]
    for index, column in enumerate(columns):
        y = 40 + index * 42
        label = html.escape(column["name"], quote=True)
        value = column["mean"]
        parts.append(f'<text x="12" y="{y + 14}">{label}</text>')
        if value is None:
            parts.append(f'<text x="430" y="{y + 14}">No observations</text>')
        else:
            width = 0.0 if scale == 0 else 200.0 * (abs(value) / scale)
            x = 440.0 - width if value < 0 else 440.0
            parts.append(f'<rect x="{x:.3f}" y="{y}" width="{width:.3f}" height="22" fill="#3568a8"/>')
            parts.append(f'<text x="{min(700.0, x + width + 6):.3f}" y="{y + 15}">{value:.8g}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _render_report(summary: dict[str, object], columns: list[dict[str, object]]) -> str:
    lines = ["# Descriptive summary", "", f"Rows: {summary['rows']}", "", "| Column | Valid | Missing | Mean | Median | Sample SD |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for column in columns:
        label = _safe_markdown_label(column["name"])
        lines.append("| " + label + " | " + " | ".join(_format_number(column[key]) for key in ("valid", "missing", "mean", "median", "sample_sd")) + " |")
    lines.extend(["", "These are descriptive statistics for the selected columns. They do not establish hypotheses, causation, or clinical conclusions.", ""])
    return "\n".join(lines)


def _safe_csv_label(value: str) -> str:
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _safe_markdown_label(value: str) -> str:
    escaped = html.escape(value, quote=True)
    for char in "\\|*_[]`":
        escaped = escaped.replace(char, "\\" + char)
    return escaped


def _format_number(value: object) -> str:
    if value is None:
        return "—"
    if type(value) in (int, float):
        return format(value, ".8g")
    raise ValueError("summary contains a nonnumeric value")


def _has_controls(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)


def _xml10_legal(value: str) -> bool:
    return all(
        0x20 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF
        for char in value
    )
