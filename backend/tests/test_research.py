from uuid import uuid4

import pytest

from scientist.auth import DomainError
from scientist.contracts import Principal
from scientist.domain import submit_run
from scientist.research import build_plan, verify_citation

# Fixture records below are explicitly labeled test data, not real publications.
DOI_RECORD = {"doi": "10.0000/Fixture.1", "title": "Fixture Study of Test Data", "year": 2020, "authors": ["A. Tester"]}
DOI_RETRIEVED = {"doi": "10.0000/fixture.1", "title": "Fixture study of test data", "year": 2020, "authors": ["A. Tester"],
                 "url": "https://example.test/fixture.1", "access": "abstract", "abstract": "Abstract text."}


def test_unretrieved_doi_is_not_verified():
    result = verify_citation({"doi": "10.0000/example"}, {})
    assert result["verification"] == "unverified"
    assert result["original_url"] is None
    assert result["access"] == "unavailable"


def test_abstract_is_not_presented_as_full_text():
    result = verify_citation({"title": "Fixture"}, {"abstract": "Text", "access": "abstract"})
    assert result["access"] == "abstract"
    # A full_text claim without an accessible full-text URL is downgraded.
    claimed = verify_citation({"doi": "10.0000/a"}, {"doi": "10.0000/a", "abstract": "Text", "access": "full_text"})
    assert claimed["access"] == "abstract"


def test_matching_retrieved_record_verifies_and_carries_provenance():
    result = verify_citation(DOI_RECORD, DOI_RETRIEVED)
    assert result["verification"] == "verified"
    assert result["identifier"] == "doi:10.0000/fixture.1"
    assert result["original_url"] == "https://example.test/fixture.1"
    assert result["discrepancies"] == []


def test_contradictory_evidence_is_flagged_not_hidden():
    wrong_year = verify_citation(DOI_RECORD, {**DOI_RETRIEVED, "year": 1999})
    assert wrong_year["verification"] == "contradictory" and wrong_year["discrepancies"] == ["year"]
    other_doi = verify_citation(DOI_RECORD, {**DOI_RETRIEVED, "doi": "10.0000/other"})
    assert other_doi["verification"] == "contradictory" and "identifier" in other_doi["discrepancies"]


def test_nonexistent_identifier_and_unsafe_url_are_not_trusted():
    assert verify_citation({"doi": "10.0000/nonexistent"}, {})["verification"] == "unverified"
    unsafe = verify_citation(DOI_RECORD, {**DOI_RETRIEVED, "url": "javascript:alert(1)"})
    assert unsafe["original_url"] is None


def _run(db, project_session):
    project_id, session_id = project_session
    owner = Principal(identity=uuid4(), kind="owner")
    return owner, submit_run(db, owner, project_id, session_id, f"k-{uuid4()}", "question", [], uuid4(), "fixture")


def test_build_plan_has_stages_for_search_verify_synthesize(db, project_session):
    owner, run = _run(db, project_session)
    plan = build_plan(db, owner, run.run_id, ["  graphene  ", "graphene", "battery"])
    assert plan.stages == ["Search literature: graphene", "Search literature: battery", "Verify references", "Synthesize evidence"]
    assert set(plan.allowed_ops) == {"search", "llm"}
    from scientist.domain import get_plan
    assert plan.input_snapshot_digest == get_plan(db, owner, run.run_id).plan.input_snapshot_digest


@pytest.mark.parametrize("terms", [[], [" "], ["x"] * 0 + ["t" * 151], [str(i) for i in range(11)]])
def test_build_plan_rejects_unbounded_terms(db, project_session, terms):
    owner, run = _run(db, project_session)
    with pytest.raises(DomainError):
        build_plan(db, owner, run.run_id, terms)


def test_build_plan_is_owner_only(db, project_session):
    owner, run = _run(db, project_session)
    with pytest.raises(DomainError, match="forbidden"):
        build_plan(db, Principal(identity=uuid4(), kind="external"), run.run_id, ["x"])


def test_verification_is_never_inferred():
    assert verify_citation({}, {"url": "https://example.test/x"})["verification"] == "unverified"
    result = verify_citation({"title": "A"}, {"doi": "10.1/x"})
    assert result["verification"] == "unverified" and result["identifier"] is None
    assert verify_citation({"title": "Same  Title"}, {"title": "same title"})["verification"] == "verified"
    assert verify_citation({"title": ""}, {"title": ""})["verification"] == "unverified"
    # Identifier is adopted from retrieved data only after a positive match.
    assert verify_citation({"title": "A"}, {"title": "B", "doi": "10.1/x"})["identifier"] is None


def test_doi_pmid_arxiv_are_normalized_and_each_claim_must_match():
    ok = verify_citation({"pmid": "PMID: 123", "arxiv": "arXiv:2101.00001v2"}, {"pmid": "123", "arxiv": "https://arxiv.org/abs/2101.00001"})
    assert ok["verification"] == "verified" and ok["identifier"] == "pmid:123"
    missing = verify_citation({"doi": "10.1/x", "pmid": "9"}, {"doi": "10.1/x"})
    assert missing["verification"] == "unverified"
    bad = verify_citation({"arxiv": "2101.00001"}, {"arxiv": "2101.99999"})
    assert bad["verification"] == "contradictory" and "identifier" in bad["discrepancies"]
    assert verify_citation({"pmid": "5"}, {"title": "T"})["verification"] == "unverified"
    pdf = verify_citation({"arxiv": "2101.00001"}, {"arxiv": "https://arxiv.org/pdf/2101.00001v2.pdf"})
    assert pdf["verification"] == "verified"
    assert verify_citation({"pmid": "9"}, {"pmid": "9", "doi": "10.9/z"})["identifier"] == "pmid:9"
    assert verify_citation({"doi": "10.1/x", "year": "99999"}, {"doi": "10.1/x"})["year"] is None


def test_year_compares_as_int_and_bad_values_do_not_crash():
    assert verify_citation({"doi": "10.1/x", "year": "2020"}, {"doi": "10.1/x", "year": 2020})["verification"] == "verified"
    junk = verify_citation({"doi": "10.1/x", "year": "abc"}, {"doi": "10.1/x", "year": 2020})
    assert junk["verification"] == "contradictory" and junk["discrepancies"] == ["year"]


def test_untitled_citation_gets_a_valid_title_and_urls_need_a_host():
    assert verify_citation({"doi": "10.1/x"}, {})["title"] == "doi:10.1/x"
    assert verify_citation({}, {})["title"] == "Untitled citation"
    for bad in ("http://", "https://[bad", "https:///path"):
        assert verify_citation({"doi": "10.1/x"}, {"doi": "10.1/x", "url": bad})["original_url"] is None
