from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from scientist.contracts import RuntimePins, ScientificBindingV2
from scientist.runtime_contracts import RUNTIME_COMMIT

from scientist.runtime_adapter import (
    RuntimeAdapterError,
    _persist_scientific_plot,
    _native_scientific_tool_definitions,
    _validate_native_tool_arguments,
)
from scientist.scientific_render import render_plot_svg


def _plot():
    return {
        "title": "Growth <week 2>",
        "x_label": "Day",
        "y_label": "Mass",
        "series": [
            {"label": "Control", "values": [1, 2, 3]},
            {"label": "Treatment", "values": [2, 1, 4]},
        ],
    }


def test_plot_svg_is_deterministic_inert_and_escapes_labels():
    first = render_plot_svg(_plot())
    assert first == render_plot_svg(_plot())
    assert "&lt;week 2&gt;" in first
    assert "<script" not in first
    assert "<polyline" in first
    ElementTree.fromstring(first)


def test_plot_tool_only_appears_for_selected_visualization_capability():
    binding = SimpleNamespace(capability_ids=["get-available-resources"])
    assert "scientific_plot" not in {
        item["name"] for item in _native_scientific_tool_definitions(binding)
    }
    binding.capability_ids.append("scientific-visualization")
    assert "scientific_plot" in {
        item["name"] for item in _native_scientific_tool_definitions(binding)
    }


def test_plot_arguments_require_selected_visualization_capability():
    with pytest.raises(RuntimeAdapterError):
        _validate_native_tool_arguments("scientific_plot", _plot(), None)
    _validate_native_tool_arguments(
        "scientific_plot", _plot(), SimpleNamespace(capability_ids=["scientific-visualization"])
    )


def test_plot_output_has_immutable_checkpoint_path_and_verified_receipt(tmp_path):
    binding = SimpleNamespace(
        max_result_bytes=32 * 1024,
        model_dump=lambda **_kwargs: {"capability_ids": ["scientific-visualization"]},
    )
    svg = render_plot_svg(_plot())
    receipt, entry = _persist_scientific_plot(tmp_path, binding, "call/one", svg)
    assert receipt.capability_id == "scientific-visualization"
    assert receipt.path.startswith("outputs/plots/") and receipt.path.endswith(".svg")
    assert entry.path == receipt.path
    assert (tmp_path / receipt.path).read_text() == svg
    assert _persist_scientific_plot(tmp_path, binding, "call/one", svg) == (receipt, entry)
    changed, _ = _persist_scientific_plot(tmp_path, binding, "call/two", svg)
    assert changed.path != receipt.path


def test_plot_persists_with_actual_chat_v2_binding(tmp_path):
    binding = ScientificBindingV2(
        binding_version=2,
        catalog_commit="154988403bb5a18e9d3c0ce4e6d5e2e4b184a298",
        registry_sha256="a" * 64,
        capability_ids=["scientific-visualization"],
        instruction_fingerprint="b" * 64,
        agent_runtime_pins=RuntimePins(
            runtime_commit=RUNTIME_COMMIT, image_digest="sha256:" + "c" * 64,
            skills_digest="d" * 64, environment_digest="e" * 64,
        ),
        input_snapshot_digest="f" * 64,
    )
    assert {tool["name"] for tool in _native_scientific_tool_definitions(binding)} == {
        "instruction_view", "scientific_plot",
    }
    svg = render_plot_svg(_plot())
    receipt, entry = _persist_scientific_plot(tmp_path, binding, "real-v2-call", svg)
    assert entry.sha256 == receipt.sha256
    assert (tmp_path / entry.path).read_text() == svg


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plot: plot["series"][0]["values"].__setitem__(0, float("nan")),
        lambda plot: plot["series"][0]["values"].__setitem__(0, 10**400),
        lambda plot: plot["series"][0].__setitem__("values", [1] * 65),
        lambda plot: plot.__setitem__("unexpected", True),
    ],
)
def test_plot_rejects_nonfinite_oversized_or_extra_input(mutate):
    plot = _plot()
    mutate(plot)
    with pytest.raises(ValueError):
        render_plot_svg(plot)
