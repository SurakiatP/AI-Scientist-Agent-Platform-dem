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


def render_plot_svg(plot: dict[str, object]) -> str:
    """Render a small, inert line chart from bounded numeric series."""
    if type(plot) is not dict or set(plot) != {"title", "x_label", "y_label", "series"}:
        raise ValueError("plot shape is invalid")
    labels = [plot[key] for key in ("title", "x_label", "y_label")]
    if any(
        not isinstance(label, str)
        or not label
        or len(label) > 100
        or _has_controls(label)
        or not _xml10_legal(label)
        for label in labels
    ):
        raise ValueError("plot label is invalid")
    series = plot["series"]
    if type(series) is not list or not 1 <= len(series) <= 3:
        raise ValueError("plot series are invalid")
    names: set[str] = set()
    for item in series:
        if type(item) is not dict or set(item) != {"label", "values"}:
            raise ValueError("plot series shape is invalid")
        label, values = item["label"], item["values"]
        if (
            not isinstance(label, str)
            or not label
            or len(label) > 64
            or _has_controls(label)
            or not _xml10_legal(label)
            or label in names
        ):
            raise ValueError("plot series label is invalid")
        names.add(label)
        if (
            type(values) is not list
            or not 2 <= len(values) <= 64
            or any(
                type(value) not in (int, float)
                or (type(value) is float and not math.isfinite(value))
                or abs(value) > 1e100
                for value in values
            )
        ):
            raise ValueError("plot values must contain 2 to 64 bounded finite numbers")

    scale = max((abs(value) for item in series for value in item["values"]), default=1.0) or 1.0
    colors = ("#3568a8", "#c34f4f", "#33845b")
    x0, x1 = 82.0, 680.0
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 410" role="img" aria-labelledby="title desc">',
        f'<title id="title">{html.escape(plot["title"], quote=True)}</title>',
        f'<desc id="desc">{html.escape(plot["y_label"], quote=True)} by {html.escape(plot["x_label"], quote=True)}</desc>',
        '<path d="M82 54V334H680" fill="none" stroke="#667085" stroke-width="1"/>',
    ]
    for series_index, item in enumerate(series):
        values = item["values"]
        points = [
            (x0 + index * (x1 - x0) / (len(values) - 1), 194.0 - (value / scale) * 126.0)
            for index, value in enumerate(values)
        ]
        encoded_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        color = colors[series_index]
        parts.append(f'<polyline points="{encoded_points}" fill="none" stroke="{color}" stroke-width="3"/>')
        for x, y in points:
            parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{color}"/>')
        legend_y = 367 + series_index * 14
        parts.append(f'<path d="M{x0} {legend_y}h18" stroke="{color}" stroke-width="3"/>')
        parts.append(
            f'<text x="{x0 + 24}" y="{legend_y + 4}" font-size="12">'
            f'{html.escape(item["label"], quote=True)}</text>'
        )
    parts.extend(
        [
            f'<text x="381" y="402" text-anchor="middle" font-size="13">{html.escape(plot["x_label"], quote=True)}</text>',
            f'<text x="18" y="194" text-anchor="middle" font-size="13" transform="rotate(-90 18 194)">{html.escape(plot["y_label"], quote=True)}</text>',
            "</svg>",
        ]
    )
    svg = "".join(parts)
    if len(svg.encode("utf-8")) > 32 * 1024:
        raise ValueError("rendered plot exceeds the byte limit")
    return svg


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
