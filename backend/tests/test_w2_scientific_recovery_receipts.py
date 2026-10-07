import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).with_name("live") / "w2_scientific_recovery_acceptance.py"
_SPEC = importlib.util.spec_from_file_location("w2_scientific_recovery_acceptance", _SCRIPT)
_RECOVERY = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_RECOVERY)


def _rows(receipt_sha256: str) -> list[dict]:
    return [
        {
            "output_index": index,
            "tool_call_id": "compute-call",
            "artifact_id": f"artifact-{index}",
            "receipt_sha256": receipt_sha256,
            "context": {"boundary": "tool_committed"},
        }
        for index in range(4)
    ]


def test_v2_outputs_share_one_receipt_digest_and_reject_divergence() -> None:
    receipt = {"receipt_version": 2, "outputs": [{"index": index} for index in range(4)]}
    digest = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()

    _RECOVERY._check_v2_receipts(_rows(digest))

    divergent = _rows(digest)
    divergent[2]["receipt_sha256"] = hashlib.sha256(b"different shared receipt").hexdigest()
    with pytest.raises(_RECOVERY.AcceptanceError, match="receipt"):
        _RECOVERY._check_v2_receipts(divergent)

    blank = _rows(digest)
    blank[2]["receipt_sha256"] = "  "
    with pytest.raises(_RECOVERY.AcceptanceError, match="receipt"):
        _RECOVERY._check_v2_receipts(blank)


def test_output_snapshot_requires_the_expected_partial_state_and_integrity() -> None:
    titles = _RECOVERY.OUTPUTS
    rows = [
        {
            "id": f"artifact-{index}",
            "title": title,
            "object_key": f"objects/{index}",
            "sha256": "a" * 64,
            "size": index + 1,
            "partial": True,
        }
        for index, title in enumerate(titles)
    ]

    class _Handle:
        def q(self, _query):
            return rows

    handle = _Handle()
    partial_snapshot = _RECOVERY._output_snapshot(handle, titles, expect_partial=True)
    assert len(partial_snapshot) == 4
    with pytest.raises(_RECOVERY.AcceptanceError):
        _RECOVERY._output_snapshot(handle, titles)

    complete_rows = [{**row, "partial": False} for row in rows]
    handle.q = lambda _query: complete_rows
    assert len(_RECOVERY._output_snapshot(handle, titles)) == 4
    with pytest.raises(_RECOVERY.AcceptanceError):
        _RECOVERY._output_snapshot(handle, titles, expect_partial=True)

    unhashed_rows = [{**row, "sha256": ""} for row in rows]
    handle.q = lambda _query: unhashed_rows
    with pytest.raises(_RECOVERY.AcceptanceError):
        _RECOVERY._output_snapshot(handle, titles, expect_partial=True)
