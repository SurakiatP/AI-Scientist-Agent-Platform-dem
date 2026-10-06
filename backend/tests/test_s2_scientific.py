import hashlib
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scientist.cpu_recipes import describe_csv
from scientist.scientific_render import render_outputs


FIXTURES = Path(__file__).parent / "fixtures" / "scientific"


def test_partial_csv_summary_and_all_four_rendered_outputs_are_deterministic():
    data = (FIXTURES / "partial.csv").read_bytes()
    summary = describe_csv(data, {"numeric_columns": ["x", "y"]})

    assert summary["rows"] == 3
    assert summary["input_sha256"] == hashlib.sha256(data).hexdigest()
    assert summary["columns"] == [
        {"name": "x", "valid": 3, "missing": 0, "min": 1.0, "max": 5.0,
         "mean": 3.0, "median": 3.0, "sample_sd": 2.0},
        {"name": "y", "valid": 2, "missing": 1, "min": 2.0, "max": 6.0,
         "mean": 4.0, "median": 4.0, "sample_sd": math.sqrt(8)},
    ]
    outputs = render_outputs(summary)
    assert list(outputs) == ["summary.json", "summary.csv", "chart.svg", "report.md"]
    assert list(render_outputs(summary).items()) == list(outputs.items())
    decoded_size = sum(len(value) for value in outputs.values())
    assert decoded_size <= 256 * 1024
    assert json.loads(outputs["summary.json"]) == summary
    assert b"x,3,0,1.0,5.0,3.0,3.0,2.0" in outputs["summary.csv"]
    assert b"y,2,1,2.0,6.0,4.0,4.0,2.8284271247461903" in outputs["summary.csv"]
    assert b">x</text>" in outputs["chart.svg"] and b">3</text>" in outputs["chart.svg"]
    assert b">y</text>" in outputs["chart.svg"] and b">4</text>" in outputs["chart.svg"]
    assert b"| x | 3 | 0 | 3 | 3 | 2 |" in outputs["report.md"]
    assert b"| y | 2 | 1 | 4 | 4 | 2.8284271 |" in outputs["report.md"]


def test_empty_cells_are_missing_and_single_observation_has_no_sample_sd():
    summary = describe_csv(b'value\n""\n7\n', {"numeric_columns": ["value"]})

    assert summary["columns"] == [
        {"name": "value", "valid": 1, "missing": 1, "min": 7.0, "max": 7.0,
         "mean": 7.0, "median": 7.0, "sample_sd": None}
    ]


@pytest.mark.parametrize(
    ("data", "params"),
    [
        (b"x,x\n1,2\n", {"numeric_columns": ["x"]}),
        (b"x,y\n1\n", {"numeric_columns": ["x"]}),
        (b"x\nNaN\n", {"numeric_columns": ["x"]}),
        (b"x\ninf\n", {"numeric_columns": ["x"]}),
        (b"x\n1\xff\n", {"numeric_columns": ["x"]}),
        (b'x\n"unterminated\n', {"numeric_columns": ["x"]}),
        (b"x\nword\n", {"numeric_columns": ["x"]}),
        (b"x\n1e999\n", {"numeric_columns": ["x"]}),
        (b"x\n1\n", {"numeric_columns": ["x"], "extra": True}),
        (b"x\n1\n", {"numeric_columns": ["x", "x"]}),
        (b"x\n1\n", {"numeric_columns": ["unknown"]}),
        (b"x\n1\n", {"numeric_columns": [True]}),
        (b"x\n1\n", {"numeric_columns": []}),
    ],
)
def test_rejects_invalid_csv_or_selection(data, params):
    with pytest.raises(ValueError):
        describe_csv(data, params)


def test_formula_like_header_is_neutralized_in_text_outputs():
    summary = describe_csv(b"=SUM(A1:A2)\n1\n", {"numeric_columns": ["=SUM(A1:A2)"]})
    outputs = render_outputs(summary)

    assert b"'=SUM(A1:A2)" in outputs["summary.csv"]
    assert b"&lt;script" not in outputs["chart.svg"]
    assert b"<script" not in outputs["chart.svg"]
    assert b"<script" not in outputs["report.md"]


def test_user_labels_are_escaped_for_svg_and_markdown():
    summary = describe_csv(b"<img onerror=1>\n2\n", {"numeric_columns": ["<img onerror=1>"]})
    outputs = render_outputs(summary)

    assert b"&lt;img onerror=1&gt;" in outputs["chart.svg"]
    assert b"&lt;img onerror=1&gt;" in outputs["report.md"]
    assert b"<img" not in outputs["chart.svg"]


def test_rejects_controls_and_out_of_bound_input():
    with pytest.raises(ValueError):
        describe_csv(b"x\n1\x00\n", {"numeric_columns": ["x"]})
    with pytest.raises(ValueError):
        describe_csv(b"x\n1\n" * 180_000, {"numeric_columns": ["x"]})


def test_accepts_bom_and_preserves_original_bytes_in_hash():
    data = b"\xef\xbb\xbfvalue\n2\n"
    summary = describe_csv(data, {"numeric_columns": ["value"]})

    assert summary["input_sha256"] == hashlib.sha256(data).hexdigest()
    assert summary["columns"][0]["mean"] == 2


def test_rejects_rows_or_headers_outside_contract_bounds():
    with pytest.raises(ValueError):
        describe_csv(b"x\n1\n" * 10_002, {"numeric_columns": ["x"]})
    with pytest.raises(ValueError):
        describe_csv(("a" * 129 + "\n1\n").encode(), {"numeric_columns": ["a" * 129]})
    headers = ",".join(f"c{i}" for i in range(9))
    with pytest.raises(ValueError):
        describe_csv((headers + "\n" + ",".join("1" for _ in range(9)) + "\n").encode(), {"numeric_columns": ["c0"]})


def test_rejects_nonfinite_computed_statistics():
    with pytest.raises(ValueError):
        describe_csv(b"x\n1.7e308\n-1.7e308\n", {"numeric_columns": ["x"]})


@pytest.mark.parametrize("value", ["1e307", "-1e307"])
def test_rendered_svg_keeps_extreme_finite_bar_geometry_finite(value):
    summary = describe_csv(f"x\n{value}\n".encode(), {"numeric_columns": ["x"]})
    svg = render_outputs(summary)["chart.svg"]

    assert b"inf" not in svg.lower() and b"nan" not in svg.lower()
    root = ET.fromstring(svg)
    for element in root.iter():
        for key in ("x", "y", "width", "height"):
            if key in element.attrib:
                assert math.isfinite(float(element.attrib[key]))


@pytest.mark.parametrize("label", ["\ufffe", "\uffff"])
def test_rejects_xml_10_illegal_header_scalars(label):
    with pytest.raises(ValueError):
        describe_csv((label + "\n1\n").encode(), {"numeric_columns": [label]})

    summary = describe_csv(b"x\n1\n", {"numeric_columns": ["x"]})
    summary["columns"][0]["name"] = label
    with pytest.raises(ValueError):
        render_outputs(summary)
