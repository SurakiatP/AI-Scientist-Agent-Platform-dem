import os


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


# SCIENTIST_PROVIDER_ENDPOINT must equal the dispatch template's provider destination exactly;
# per-provider_id keying is a follow-up.
def provider_endpoint() -> str | None:
    return _origin(os.environ.get("SCIENTIST_PROVIDER_ENDPOINT", ""))


def allowed_recipients() -> set[str]:
    provider = provider_endpoint()
    return set(scholarly_endpoints()) | ({provider} if provider else set())
