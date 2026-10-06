from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace
import pytest

from scientist.compute_runtime import _DOCKER_CONTEXT, _parse_archive, _run_docker


def archive_for(files: dict[str, tuple[bytes, bool]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, (data, regular) in files.items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.REGTYPE if regular else tarfile.SYMTYPE
            info.linkname = "report.md" if not regular else ""
            info.size = len(data) if regular else 0
            archive.addfile(info, io.BytesIO(data) if regular else None)
    return output.getvalue()


def outputs() -> dict[str, tuple[bytes, bool]]:
    return {
        "summary.json": (b'{"rows":3}', True),
        "summary.csv": (b"name,value\nrows,3\n", True),
        "chart.svg": (
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 140" role="img" aria-labelledby="title desc">'
            b'<title id="title">Means</title><desc id="desc">CSV means</desc>'
            b'<text x="12" y="54">x</text><rect x="440.000" y="40" width="150.000" height="22" fill="#3568a8"/>'
            b'</svg>',
            True,
        ),
        "report.md": (b"# Report\n", True),
    }


def test_guest_svg_cannot_smuggle_active_content_into_receipt() -> None:
    payload = outputs()
    payload["chart.svg"] = (b'<svg><script>alert(1)</script></svg>', True)

    with pytest.raises(ValueError, match="active or external"):
        _parse_archive(archive_for(payload))


def test_guest_json_cannot_overflow_to_nonfinite_float() -> None:
    payload = outputs()
    payload["summary.json"] = (b'{"value":1e999}', True)

    with pytest.raises(ValueError, match="non-finite"):
        _parse_archive(archive_for(payload))


def test_guest_svg_animation_cannot_change_static_chart_content() -> None:
    payload = outputs()
    payload["chart.svg"] = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 140" role="img" aria-labelledby="title desc">'
        b'<title id="title">x</title><desc id="desc">x</desc>'
        b'<animate attributeName="href" values="https://bad"/></svg>',
        True,
    )

    with pytest.raises(ValueError, match="active"):
        _parse_archive(archive_for(payload))


def test_guest_svg_external_stylesheet_processing_instruction_is_rejected() -> None:
    payload = outputs()
    chart, regular = payload["chart.svg"]
    payload["chart.svg"] = (
        b'<?xml-stylesheet type="text/css" href="https://evil.invalid/style.css"?>' + chart,
        regular,
    )

    with pytest.raises(ValueError, match="processing instruction"):
        _parse_archive(archive_for(payload))


def test_guest_symlink_and_unreviewed_output_are_rejected() -> None:
    payload = outputs()
    payload["report.md"] = (b"", False)

    with pytest.raises(ValueError, match="unknown or non-regular"):
        _parse_archive(archive_for(payload))

    payload = outputs()
    payload["extra.txt"] = (b"unexpected", True)
    with pytest.raises(ValueError, match="exactly four"):
        _parse_archive(archive_for(payload))


def test_oversized_guest_output_is_rejected_from_tar_headers() -> None:
    payload = outputs()
    payload["report.md"] = (b"x" * (256 * 1024), True)
    with pytest.raises(ValueError, match="256 KiB"):
        _parse_archive(archive_for(payload))


def test_compute_docker_calls_use_owned_context_and_five_second_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr("scientist.compute_runtime.subprocess.run", run)

    assert _run_docker(SimpleNamespace(context=_DOCKER_CONTEXT), "inspect") == "ok"
    assert calls[0][0] == ["docker", "--context", _DOCKER_CONTEXT, "inspect"]
    assert calls[0][1]["timeout"] == 5

    with pytest.raises(RuntimeError, match="owned Docker context"):
        _run_docker(SimpleNamespace(context="other"), "inspect")
