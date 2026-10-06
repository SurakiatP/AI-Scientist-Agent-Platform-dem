import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from scientist.contracts import CrossrefQueryV1
from scientist.scholarly_retrieval import build_crossref_url, parse_crossref_response


FIXTURES = Path(__file__).parent / "fixtures" / "crossref"


def request(**changes):
    values = dict(
        source_id="crossref", version=1, access_mode="public_read",
        query="gene expression", doi=None, limit=5,
    )
    values.update(changes)
    return CrossrefQueryV1(**values)


def body(name):
    return (FIXTURES / name).read_bytes()


def test_builds_only_crossref_query_and_normalized_doi_routes():
    assert build_crossref_url(request(query="gene expression & cells", limit=3)) == (
        "https://api.crossref.org/works?query=gene+expression+%26+cells&rows=3"
    )
    doi_request = request(query=None, doi="https://doi.org/10.5555/Fixture.1")
    assert doi_request.doi == "10.5555/fixture.1"
    assert build_crossref_url(doi_request) == "https://api.crossref.org/works/10.5555%2Ffixture.1"
    with pytest.raises((TypeError, ValueError, ValidationError)):
        build_crossref_url({"query": "https://attacker.test", "limit": 2})
    with pytest.raises(ValidationError):
        CrossrefQueryV1.model_validate({
            "source_id": "crossref", "version": 1, "access_mode": "public_read",
            "query": "paper", "doi": None, "limit": 2, "url": "https://attacker.test",
            "cursor": "next",
        })
    with pytest.raises(ValidationError):
        request(query="paper\n&rows=999")


def test_parses_crossref_query_records_without_inventing_full_text_or_doi():
    result = parse_crossref_response(request(), body("query.json"))
    assert result["source_id"] == "crossref" and result["access_mode"] == "public_read"
    record = result["records"][0]
    assert record["doi"] == "10.5555/FiXtUrE.1"
    assert record["title"] == "<i>Fixture</i> paper"  # retained as text, never rendered as HTML here
    assert record["authors"] == ["Ada Tester"] and record["year"] == 2024
    assert record["url"] == "https://doi.org/10.5555/FiXtUrE.1"
    assert record["access"] == "metadata_only" and record["full_text_status"] == "unknown"
    assert record["provenance"] == {"source_id": "crossref", "doi": "10.5555/FiXtUrE.1"}
    assert "verification" in record  # identity metadata is not claim support


def test_single_doi_requires_identity_match_and_reports_missing_metadata():
    query = request(query=None, doi="10.5555/fixture.1")
    record = parse_crossref_response(query, body("doi.json"))["records"][0]
    assert record["doi"] == "10.5555/fixture.1"
    assert record["authors"] == ["Crossref Test Group"]
    assert "title" not in record["omissions"]
    assert "year" in record["omissions"]
    missing = parse_crossref_response(request(), body("missing-metadata.json"))["records"][0]
    assert missing["doi"] is None and missing["title"] is None and missing["authors"] == []
    assert {"doi", "title", "authors", "year", "url"} <= set(missing["omissions"])
    assert missing["provenance"]["doi"] is None


@pytest.mark.parametrize("payload", [
    b"{\"status\":\"ok\",\"message-type\":\"work\",\"message-type\":\"work\",\"message\":{}}",
    b"{\"status\":\"ok\",\"message-type\":\"work-list\",\"message\":{\"items\":[NaN]}}",
    b"\xff",
    b"{}",
])
def test_rejects_malformed_or_unknown_envelopes(payload):
    with pytest.raises(ValueError):
        parse_crossref_response(request(), payload)


def test_rejects_quota_wrong_envelope_mismatched_doi_and_unbounded_body():
    with pytest.raises(ValueError, match="Crossref error"):
        parse_crossref_response(request(), body("quota-error.json"))
    with pytest.raises(ValueError, match="envelope"):
        parse_crossref_response(request(), body("wrong-envelope.json"))
    wrong = request(query=None, doi="10.5555/other.1")
    with pytest.raises(ValueError, match="does not match"):
        parse_crossref_response(wrong, body("doi.json"))
    with pytest.raises(ValueError, match="size"):
        parse_crossref_response(request(), b" " * (1_048_577))


def test_rejects_response_controls_and_record_count_over_approved_limit():
    response = json.loads(body("query.json"))
    response["message"]["items"] *= 6
    with pytest.raises(ValueError, match="record limit"):
        parse_crossref_response(request(), json.dumps(response).encode())
    response["message"]["items"] = [{"title": ["bad\nheader"]}]
    with pytest.raises(ValueError, match="control"):
        parse_crossref_response(request(), json.dumps(response).encode())


def test_bounds_json_nesting_and_text_depth():
    response = json.loads(body("query.json"))
    nested = "leaf"
    for _ in range(17):
        nested = {"next": nested}
    response["extra"] = nested
    with pytest.raises(ValueError, match="nesting"):
        parse_crossref_response(request(), json.dumps(response).encode())


def test_does_not_coerce_malformed_years_or_url_controls():
    response = json.loads(body("query.json"))
    response["message"]["items"][0]["published"]["date-parts"][0][0] = 2024.5
    record = parse_crossref_response(request(), json.dumps(response).encode())["records"][0]
    assert record["year"] is None and "year" in record["omissions"]
    response["message"]["items"][0]["URL"] = "https://example.test/\nunsafe"
    with pytest.raises(ValueError, match="control"):
        parse_crossref_response(request(), json.dumps(response).encode())
