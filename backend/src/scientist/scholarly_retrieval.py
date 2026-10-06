"""Bounded Crossref request formatting and metadata parsing; no network access."""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urlencode

from scientist.contracts import CrossrefQueryV1, normalize_doi
from scientist.research import _safe_url, _year, verify_citation

_ROOT = "https://api.crossref.org/works"
_MAX_BODY = 1_048_576
_MAX_DEPTH = 16
_MAX_TEXT = 65_536
_DOI = re.compile(r"10\.[0-9]{4,9}/[^\s<>\"']+\Z")


def _validate_request(request: CrossrefQueryV1) -> tuple[str | None, str | None, int]:
    if (not isinstance(request, CrossrefQueryV1) or getattr(request, "model_extra", None)
            or request.source_id != "crossref" or request.version != 1
            or request.access_mode != "public_read"):
        raise ValueError("invalid Crossref request")
    if isinstance(request.limit, bool) or not isinstance(request.limit, int) or not 1 <= request.limit <= 20:
        raise ValueError("invalid Crossref limit")
    query, doi = request.query, request.doi
    if (query is None) == (doi is None):
        raise ValueError("exactly one Crossref query or DOI is required")
    if query is not None:
        if not isinstance(query, str) or not query.strip() or len(query) > 512 or _has_control(query):
            raise ValueError("invalid Crossref query")
    else:
        doi = normalize_doi(doi)
        if doi is None or len(doi) > 255 or _has_control(doi) or not _DOI.fullmatch(doi):
            raise ValueError("invalid Crossref DOI")
    return query, doi, request.limit


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)


def build_crossref_url(request: CrossrefQueryV1) -> str:
    """Build only the approved Crossref query or DOI URL; performs no request."""
    query, doi, limit = _validate_request(request)
    if doi is not None:
        return f"{_ROOT}/{quote(doi, safe='')}"
    return f"{_ROOT}?{urlencode({'query': query, 'rows': limit})}"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _check_json_bounds(value: object) -> None:
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > _MAX_DEPTH:
            raise ValueError("Crossref response nesting is too deep")
        if isinstance(current, str) and len(current) > _MAX_TEXT:
            raise ValueError("Crossref response text is too long")
        if isinstance(current, dict):
            if any(not isinstance(key, str) or len(key) > _MAX_TEXT for key in current):
                raise ValueError("Crossref response key is too long")
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)


def _text(value: object, field: str, maximum: int) -> str:
    if isinstance(value, str) and _has_control(value):
        raise ValueError(f"Crossref {field} contains a control character")
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"invalid Crossref {field}")
    return value


def _record(item: dict[str, object], *, requested_doi: str | None) -> dict[str, object]:
    omissions: list[str] = []
    raw_doi = item.get("DOI")
    if raw_doi is not None:
        raw_doi = _text(raw_doi, "DOI", 255)
        normalized_doi = normalize_doi(raw_doi)
        if normalized_doi is None or not _DOI.fullmatch(normalized_doi):
            raise ValueError("invalid Crossref DOI metadata")
    else:
        normalized_doi = None
        omissions.append("doi")
    if requested_doi is not None and normalized_doi != requested_doi:
        raise ValueError("Crossref DOI response does not match requested DOI")

    title_value = item.get("title")
    if title_value is None:
        title = None
        omissions.append("title")
    elif isinstance(title_value, list) and (not title_value or isinstance(title_value[0], str)):
        title = _text(title_value[0], "title", 1000) if title_value else None
        if title is None:
            omissions.append("title")
    else:
        raise ValueError("invalid Crossref title metadata")

    authors_value = item.get("author")
    authors: list[str] = []
    if authors_value is None:
        omissions.append("authors")
    elif not isinstance(authors_value, list) or len(authors_value) > 200:
        raise ValueError("invalid Crossref author metadata")
    else:
        for author in authors_value:
            if not isinstance(author, dict):
                raise ValueError("invalid Crossref author metadata")
            name = author.get("name")
            if name is None:
                given, family = author.get("given"), author.get("family")
                parts = [_text(part, "author name", 150) for part in (given, family) if part is not None]
                name = " ".join(parts)
            else:
                name = _text(name, "author name", 300)
            if name:
                authors.append(name)
        if not authors:
            omissions.append("authors")

    year = None
    for date_key in ("published", "published-print", "published-online", "issued"):
        date = item.get(date_key)
        if isinstance(date, dict):
            parts = date.get("date-parts")
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                value = parts[0][0]
                year = _year(value) if isinstance(value, int) and not isinstance(value, bool) else None
                if year is not None:
                    break
    if year is None:
        omissions.append("year")

    raw_url = item.get("URL")
    if isinstance(raw_url, str) and _has_control(raw_url):
        raise ValueError("Crossref URL contains a control character")
    url = _safe_url(raw_url)
    if url is not None:
        _text(url, "URL", 2048)
    else:
        omissions.append("url")

    identity = verify_citation({"doi": requested_doi}, {"doi": raw_doi}) if requested_doi else None
    return {
        "doi": raw_doi,
        "title": title,
        "authors": authors,
        "year": year,
        "url": url,
        "access": "metadata_only",
        "full_text_status": "unknown",
        "verification": identity["verification"] if identity else "unverified",
        "omissions": omissions,
        "provenance": {"source_id": "crossref", "doi": raw_doi},
    }


def parse_crossref_response(request: CrossrefQueryV1, body: bytes) -> dict[str, object]:
    """Parse a bounded Crossref response body without making HTTP requests."""
    query, requested_doi, limit = _validate_request(request)
    if not isinstance(body, bytes) or len(body) > _MAX_BODY:
        raise ValueError("Crossref response size is invalid")
    try:
        payload = json.loads(body.decode("utf-8", "strict"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid Crossref JSON response") from exc
    _check_json_bounds(payload)
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ValueError("Crossref error response or wrong envelope")
    message = payload.get("message")
    expected_type = "work" if requested_doi is not None else "work-list"
    if payload.get("message-type") != expected_type or not isinstance(message, dict):
        raise ValueError("Crossref response envelope does not match request")
    if requested_doi is not None:
        items = [message]
    else:
        items = message.get("items")
        if not isinstance(items, list):
            raise ValueError("Crossref work-list envelope is missing items")
        if len(items) > limit:
            raise ValueError("Crossref response exceeds approved record limit")
    records = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Crossref record is not an object")
        records.append(_record(item, requested_doi=requested_doi))
    approved_request = {"query": query, "doi": requested_doi, "limit": limit}
    return {
        "source_id": "crossref",
        "access_mode": "public_read",
        "provenance": {"source_id": "crossref", "access_mode": "public_read", "request": approved_request},
        "record_count": len(records),
        "records": records,
    }
