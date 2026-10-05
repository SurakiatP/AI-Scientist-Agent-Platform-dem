"""Dependency boundary for the MCP/A2A SDK additions (I2-A4)."""
import importlib.metadata as md
import subprocess
import sys

# License reviewed 2026-10-05; packages without usable metadata were read from their LICENSE file.
ALLOWED = {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0", "MPL-2.0"}
NEW_PACKAGES = {
    "a2a-sdk": "Apache-2.0", "mcp": "MIT", "mcp-types": "MIT", "httpx2": "BSD-3-Clause",
    "httpcore2": "BSD-3-Clause", "charset-normalizer": "MIT", "google-api-core": "Apache-2.0",
    "google-auth": "Apache-2.0", "googleapis-common-protos": "Apache-2.0", "json-rpc": "MIT",
    "proto-plus": "Apache-2.0", "protobuf": "BSD-3-Clause", "pyasn1": "BSD-2-Clause",
    "pyasn1-modules": "BSD-2-Clause", "pyjwt": "MIT", "python-multipart": "Apache-2.0",
    "requests": "Apache-2.0", "sse-starlette": "BSD-3-Clause", "truststore": "MIT",
}
_CLASSIFIERS = {"MIT License": "MIT", "Apache Software License": "Apache-2.0", "BSD License": None,
                "ISC License (ISCL)": "ISC", "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0"}


def test_new_packages_have_allowed_licenses():
    assert set(NEW_PACKAGES.values()) <= ALLOWED
    for name, expected in NEW_PACKAGES.items():
        meta = md.metadata(name)
        declared = meta.get("License-Expression")
        if declared:
            assert declared == expected, name
        for c in meta.get_all("Classifier") or []:
            if c.startswith("License ::"):
                mapped = _CLASSIFIERS.get(c.split(" :: ")[-1], "unknown")
                assert mapped in (None, expected), (name, c)


def test_sdk_apis_import():
    from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes  # noqa: F401
    from a2a.types import a2a_pb2  # noqa: F401
    from mcp.server import MCPServer

    assert MCPServer("x").streamable_http_app(streamable_http_path="/mcp") is not None


def test_private_dispatch_does_not_load_protocol_sdks():
    code = (
        "import sys, scientist.private_dispatch_entrypoint\n"
        "bad = {m.split('.')[0] for m in sys.modules} & "
        "{'mcp','mcp_types','a2a','httpx2','httpcore2','dispatch','server'}\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
