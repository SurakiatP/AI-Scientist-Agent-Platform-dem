import json
import os
from collections.abc import Mapping
from typing import Any
from uuid import UUID


DATABASE_URL = os.environ.get(
    "SCIENTIST_DATABASE_URL",
    "postgresql+psycopg:///?dbname=scientist&host=/var/run/postgresql",
)


def _origin(value: str) -> str | None:
    """Canonical `https://host` origin of a literal endpoint, or None when unsafe.

    Rejects userinfo/`@`, any explicit port (including 443), localhost names, metadata hosts,
    non-canonical numeric IPv4 (`2130706433`, `127.1`) and non-global IP literals.
    """
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    from scientist.broker import _METADATA_HOSTS
    try:
        parts = urlsplit(value.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or not host or port is not None or "@" in parts.netloc:
        return None
    host = host.rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost") or host in _METADATA_HOSTS:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            socket.inet_aton(host)
        except OSError:
            return f"https://{host}"
        return None  # numeric spelling that ip_address refuses is never a canonical IPv4
    return None if not ip.is_global else f"https://[{host}]" if ip.version == 6 else f"https://{host}"


def scholarly_endpoints() -> list[str]:
    raw = os.environ.get("SCIENTIST_SCHOLARLY_ENDPOINTS", "")
    return list(dict.fromkeys(o for v in raw.split(",") if (o := _origin(v))))


_MAX_DESTINATIONS_BYTES = 16 * 1024
_MAX_PEER_DESTINATIONS = 100


def _unique_peer_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate peer configuration key")
        result[key] = value
    return result


def parse_peer_destinations(value: str | Mapping[str, str] | None) -> dict[str, str]:
    """Parse the canonical, bounded peer UUID-to-HTTPS-origin map fail closed."""
    if value is None or value == "":
        return {}
    try:
        if isinstance(value, str):
            if len(value.encode("utf-8")) > _MAX_DESTINATIONS_BYTES:
                return {}
            parsed = json.loads(value, object_pairs_hook=_unique_peer_pairs)
        elif isinstance(value, Mapping):
            parsed = dict(value)
            encoded = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(encoded) > _MAX_DESTINATIONS_BYTES:
                return {}
        else:
            return {}
        if not isinstance(parsed, dict) or len(parsed) > _MAX_PEER_DESTINATIONS:
            return {}
        result: dict[str, str] = {}
        for peer_id, endpoint in parsed.items():
            if not isinstance(peer_id, str) or str(UUID(peer_id)) != peer_id:
                return {}
            if peer_id in result or not isinstance(endpoint, str):
                return {}
            origin = _origin(endpoint)
            if origin is None or origin != endpoint:
                return {}
            result[peer_id] = origin
        return result
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return {}


def peer_destinations() -> dict[str, str]:
    """Read SCIENTIST_PEER_DESTINATIONS using the shared strict peer parser."""
    return parse_peer_destinations(os.environ.get("SCIENTIST_PEER_DESTINATIONS", ""))


# The broker/dispatch template keep their own provider map; if it disagrees with this one, dispatch
# fails closed (403) and the approved plan never runs, so operators must configure both identically.
def provider_destinations() -> dict[str, str]:
    """`{provider_id: origin}` from SCIENTIST_PROVIDER_DESTINATIONS; any invalid entry fails the whole map closed."""
    import json
    from uuid import UUID
    raw = os.environ.get("SCIENTIST_PROVIDER_DESTINATIONS", "")
    if not raw or len(raw.encode()) > _MAX_DESTINATIONS_BYTES:
        return {}
    try:
        # Exact duplicate keys void the map (json.loads would silently keep the last one).
        data = json.loads(raw, object_pairs_hook=lambda pairs: dict(pairs) if len(dict(pairs)) == len(pairs) else None)
        if not isinstance(data, dict):
            return {}
        out = {str(UUID(k)): v for k, v in data.items()}
        if len(out) != len(data) or any(str(UUID(k)) != k.lower() for k in data) or any(not isinstance(v, str) or _origin(v) != v for v in out.values()):
            return {}
        return out
    except (ValueError, TypeError):
        return {}


def provider_endpoint(provider_id) -> str | None:
    try:
        return provider_destinations().get(str(provider_id).lower())
    except (ValueError, TypeError):
        return None


def allowed_recipients(provider_id) -> set[str]:
    provider = provider_endpoint(provider_id)
    return set(scholarly_endpoints()) | ({provider} if provider else set())
