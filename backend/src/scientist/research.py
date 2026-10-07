"""Research plan construction and citation verification (no provider calls here)."""
from __future__ import annotations

import re
from sqlalchemy import text
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.orm import Session

from scientist import settings
from scientist.auth import DomainError
from scientist.contracts import CsvResearchSelection, CsvDescribeGrantV1, ObjectRef, PlanSpec, Principal, ScientificBindingV2
from scientist.domain import get_plan

_MAX_TERMS, _MAX_TERM_LEN = 10, 150


def build_plan(db: Session, owner: Principal, run_id: UUID, search_terms: list[str], *, workflow: str = 'literature', csv_selection: CsvResearchSelection | None = None) -> PlanSpec:
    """Draft the initial workflow: search literature, verify references, synthesize evidence.

    Keeps the run's snapshot digest, provider/model and owner-set limits; the caller
    persists the draft through domain.revise_plan so the revision check stays authoritative.
    """
    if workflow == 'crossref_csv':
        if csv_selection is None or search_terms:
            raise DomainError('invalid_request', 400)
        return _csv_plan(db, owner, run_id, csv_selection)
    if csv_selection is not None:
        raise DomainError('invalid_request', 400)
    if workflow == 'chat':
        if search_terms:
            raise DomainError('invalid_request', 400)
        from scientist.scientific_authority import visualization_binding
        current = get_plan(db, owner, run_id).plan
        provider = settings.provider_endpoint(current.provider_id)
        if not provider:
            raise DomainError('data_destinations_not_configured', 409)
        return current.model_copy(update={
            'scientific': visualization_binding(current.input_snapshot_digest),
            'stages': ['Discuss the question', 'Render approved graphs when useful'],
            'allowed_ops': ['llm'],
            'data_recipients': [provider],
            'packages': [],
        })
    if workflow == 'resources':
        from scientist.scientific_authority import resource_binding
        current = get_plan(db, owner, run_id).plan
        provider = settings.provider_endpoint(current.provider_id)
        if not provider:
            raise DomainError('data_destinations_not_configured', 409)
        return current.model_copy(update={
            'scientific': resource_binding(current.input_snapshot_digest),
            'stages': ['Measure workspace resources', 'Explain measured limits'],
            'allowed_ops': ['llm'], 'data_recipients': [provider], 'packages': [],
        })
    if workflow != 'literature':
        raise DomainError('invalid_request', 400)
    terms = list(dict.fromkeys(t.strip() for t in search_terms if isinstance(t, str) and t.strip()))
    if not terms or len(terms) > _MAX_TERMS or any(len(t) > _MAX_TERM_LEN for t in terms):
        raise DomainError("forbidden", 400)
    current = get_plan(db, owner, run_id).plan
    stages = [f"Search literature: {t}" for t in terms] + ["Verify references", "Synthesize evidence"]
    # Destinations come only from validated configuration, never from the request or search terms.
    provider, scholarly = settings.provider_endpoint(current.provider_id), settings.scholarly_endpoints()
    if not provider or not scholarly:
        raise DomainError("data_destinations_not_configured", 409)
    return current.model_copy(update={"stages": stages, "allowed_ops": ["search", "llm"], "data_recipients": [*scholarly, provider], "scientific": None})


def _csv_plan(db: Session, owner: Principal, run_id: UUID, requested: CsvResearchSelection) -> PlanSpec:
    from scientist import scientific_authority as authority
    from scientist.capability_registry import load_registry
    from scientist.instruction_loader import load_instruction_bundle, load_instruction_pins

    current = get_plan(db, owner, run_id).plan
    snapshot = db.execute(text("SELECT project_id, manifest FROM input_snapshots WHERE run_id=:run AND digest=:digest"),
                          {'run': run_id, 'digest': current.input_snapshot_digest}).one()
    captured = next((item for item in snapshot.manifest.get('files', [])
                     if item.get('id') == str(requested.csv_file_id)), None)
    if captured is None or captured.get('state', 'ready') != 'ready' or captured.get('content_type') != 'text/csv':
        raise DomainError('scientific_input_unavailable', 409)
    try:
        ref = ObjectRef(project_id=snapshot.project_id, key=captured['object_key'],
                        sha256=captured['sha256'], size=captured['size'], content_type='application/octet-stream')
        profile, manifest_hash = authority.trusted_compute_profile()
        grant = CsvDescribeGrantV1(recipe_id='csv.describe.v1', recipe_version='1',
            recipe_manifest_sha256=manifest_hash, profile_id=profile.profile_id,
            profile_version=profile.version, image_digest=profile.image_digest,
            input_ref=ref, input_sha256=ref.sha256, numeric_columns=requested.numeric_columns)
        if not authority._grant_matches_snapshot_file(db, snapshot.project_id, grant, [captured]):
            raise DomainError('scientific_input_unavailable', 409)
        registry_path = authority.ROOT / 'docs/skills/capability-registry.json'
        if not registry_path.is_file():
            registry_path = authority.ROOT / 'runtime/capability-registry.json'
        selection = load_registry(registry_path).select(['paper-lookup', 'exploratory-data-analysis'])
        instructions = load_instruction_bundle(selection, authority._bundle_root,
            load_instruction_pins(authority.ROOT / 'runtime/skills-manifest.json'),
            token_counter=lambda value: len(value.encode('utf-8')), token_budget=65536)
        binding = ScientificBindingV2(binding_version=2, catalog_commit=selection.catalog_commit,
            registry_sha256=selection.registry_sha256, capability_ids=list(selection.capability_ids),
            instruction_fingerprint=instructions.instruction_fingerprint,
            agent_runtime_pins=authority._current_runtime_pins(), input_snapshot_digest=current.input_snapshot_digest,
            approved_crossref_queries={'crossref': requested.crossref}, required_compute_profiles=[profile],
            csv_describe_grants={'csv_describe': grant})
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise DomainError('scientific_binding_unavailable', 409) from exc
    provider = settings.provider_endpoint(current.provider_id)
    if not provider or 'https://api.crossref.org' not in settings.scholarly_endpoints():
        raise DomainError('data_destinations_not_configured', 409)
    return current.model_copy(update={'scientific': binding,
        'stages': ['Retrieve Crossref metadata', 'Describe approved CSV columns', 'Explain evidence and outputs'],
        'allowed_ops': ['search', 'compute', 'llm'], 'data_recipients': ['https://api.crossref.org', provider], 'packages': []})


def _norm_id(kind: str, value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    v = str(value).strip().lower()
    if kind == "doi":
        v = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", v)
    elif kind == "pmid":
        v = re.sub(r"^(pmid:?)\s*", "", v)
    else:
        v = re.sub(r"^(https?://arxiv\.org/(abs|pdf)/|arxiv:)\s*", "", v)
        v = re.sub(r"(v\d+)?(\.pdf)?$", "", v)
    return v.strip() or None


def _ids(record: dict) -> dict[str, str]:
    found = {k: _norm_id(k, record.get(k)) for k in ("doi", "pmid", "arxiv")}
    return {k: v for k, v in found.items() if v}


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
        return value if parts.scheme in {"http", "https"} and parts.hostname else None
    except ValueError:
        return None


def _title(value: object) -> str:
    return re.sub(r"\W+", " ", str(value or "")).strip().lower()


def _year(value: object) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1000 <= year <= 9999 else None  # CitationView bounds


def verify_citation(record: dict, retrieved_metadata: dict) -> dict:
    """Verified only on a positive identity match against retrieved data; never inferred."""
    claimed, found = _ids(record), _ids(retrieved_metadata)
    url = _safe_url(retrieved_metadata.get("url"))
    abstract = bool(retrieved_metadata.get("abstract"))
    if retrieved_metadata.get("access") == "full_text" and url and not abstract:
        access = "full_text"
    elif abstract:
        access = "abstract"  # abstract is never presented as full text
    elif retrieved_metadata:
        access = "metadata"
    else:
        access = "unavailable"
    discrepancies: list[str] = []
    matched = False
    if claimed:
        if any(k in found and found[k] != v for k, v in claimed.items()):
            discrepancies.append("identifier")
        matched = not discrepancies and all(k in found for k in claimed)
    else:
        t = _title(record.get("title"))
        matched = bool(t) and t == _title(retrieved_metadata.get("title"))
    if matched or discrepancies:
        claimed_year, found_year = record.get("year"), retrieved_metadata.get("year")
        if claimed_year is not None and found_year is not None and _year(claimed_year) != _year(found_year):
            discrepancies.append("year")
        if claimed and _title(record.get("title")) and _title(retrieved_metadata.get("title")) and _title(record["title"]) != _title(retrieved_metadata["title"]):
            discrepancies.append("title")
    verification = "contradictory" if discrepancies else "verified" if matched else "unverified"
    identifier = None
    if matched:
        kind = next((k for k in ("doi", "pmid", "arxiv") if k in claimed and k in found), None) or next(
            (k for k in ("doi", "pmid", "arxiv") if k in found), None)
        identifier = f"{kind}:{found[kind]}" if kind else None
    claimed_text = next((f"{k}:{v}" for k, v in claimed.items()), None)
    return {
        "title": retrieved_metadata.get("title") or record.get("title") or identifier or claimed_text or "Untitled citation",
        "authors": retrieved_metadata.get("authors") or record.get("authors") or [],
        "year": _year(retrieved_metadata.get("year")) or _year(record.get("year")),
        "identifier": identifier,
        "original_url": url,
        "access": access,
        "verification": verification,
        "discrepancies": discrepancies,
    }
