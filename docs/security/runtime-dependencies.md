# Runtime dependency security

The worker uses Python 3.14.7 and the core dependency graph from upstream runtime commit bd0affe5e5f723579df8902852f5d0c47795f355. Platform Python remains 3.13.14. Optional providers, native dashboards, gateways, and development dependencies are excluded.

runtime/requirements.lock preserves exact target dependency versions and distribution hashes. Install with UV, --require-hashes, --no-deps and --only-binary=:all:. Two security overrides replace upstream pins: PyJWT 2.14.0 fixes CVE-2026-102266, 102267, 102268, 102271, 102272 and 102273; urllib3 2.8.0 fixes CVE-2026-97687 and 97689. Upstream source remains unchanged. The patched SDK and native dependencies import successfully in the isolated dependency image. Full pinned-runtime behavioral compatibility remains a B5 acceptance gate.

The native runtime base is signed distroless cc-debian13:nonroot, index sha256:e792ab3d241a468a4fd7519ddbbebe66b49b5f365771716ea688ad40b6c6f1c2. The official Astral Python 3.14.7 archive SHA256 is f278d66c15e81f6c77719892e3eb32ff4bca5fc4ab424e171d14bc12bb88b4ee. Unused installer bundles are excluded from the runtime input. Required shared libraries are retained.

Dependency-only image sha256:464f2609709047efeed7361f28e58aff5c300854d8a271c051b8b27db32063f3 was scanned on 2026-10-03: 14 OS packages and 66 Python distributions, zero HIGH/CRITICAL findings. This evidence covers that image only. The final application and worker images require fresh scans and SPDX SBOMs. Evidence is retained locally outside Git.

The upstream package metadata reports version 0.0.0, which triggers CVE-2026-53869 for native WebSocket dashboard endpoints. The platform must never expose or start the upstream dashboard/gateway, including /api/pty, /api/ws, /api/pub and /api/events. Platform REST, MCP and A2A adapters have separate owner/token boundaries. Actual entrypoint and network tests must prove the upstream endpoint path is absent before this control can be accepted. A metadata version edit is not a security fix.

The preparation dependency image adds the application-pinned pypdf6.19.0, openpyxl3.1.5 and et-xmlfile2.0.0. Image sha256:22e3b15ab19a7209a501b2834a3d722604d8e5b963dad980af3fdc64a4148477 scans14OS+69Python packages0HIGH/CRITICAL; actual SDK/PDF/XLSX imports pass under isolated constraints. runtime/requirements.lock includes these three packages with hashes. This remains a dependency prerequisite; actual bounded preparation execution is a B5 gate.

The PostgreSQL18.6 Alpine candidate index sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873 contains HIGH Go stdlib findings in its unused gosu privilege-switch helper. The fixed prerequisite image removes that binary and uses USER postgres (UID/GID70) directly, retaining the unchanged PostgreSQL server and standard entrypoint. Image sha256:a86add9f159a3628a029db26cf4652d0ae189d583642e3409ff1193a43a5dac6 scans53OS packages0HIGH/CRITICAL; postgres --version returns18.6 and runtime id confirms70. Final deployment must keep the non-root default and independently validate initialization, credentials, encrypted persistence and restore. Native host test PostgreSQL18.6 remains separate.

## Backend dependency prerequisite

The isolated backend image uses Python3.13.14 from the official Astral20260805 ARM64 archive (SHA2564777d7df2edb47b96e53abad5e1b9df1b2a1a9b2f7bdba12b5c0122163b3fed9) and the existing signed cc-debian13:nonroot base index. Backend requirements are exported with hashes from the checked uv.lock and installed for LinuxARM64. Unused bundled pip/ensurepip and share/terminfo data are excluded; the application still targets Python>=3.13,<3.14.

Owned-image prerequisite scientist-server-prerequisite:test is sha256:4912c149defb100b6c48c2d2674d98f0cd52183b1727ad26fd6fe3d260b696af. Its SPDX inventory has49 packages and the vulnerability scan contains0 HIGH/CRITICAL findings. Actual native Python, SQLAlchemy, psycopg, Pydantic, FastAPI, Uvicorn, httpx, cryptography, boto3, PDF and XLSX imports passed in a nonroot/read-only/network-none/cap-drop container with limits. Local evidence is .local/security/server-prerequisite.json and server-prerequisite.spdx.json. This image contains dependencies only; actual private-service composition, PostgreSQL init/persistence, source image, native runtime, containment and restore remain separate pending gates.

## Pinned native-source import prerequisite

The owned Engine29.8.2 build added the exact git archive of runtime commit
bd0affe5e5f723579df8902852f5d0c47795f355 to the reviewed dependency image,
plus the three selected skill directories (67 regular files, all manifest sizes
and SHA-256 values reverified before copying). Source archive SHA-256:
e97403574699e253952c14dcfe03682f62985f59f529d624d92553694c7e8fea.

Source-only prerequisite image:
sha256:658c35b6d48ad195df0afefd6037419b85a4ddf226e27fed783a9b7326c050e6.
Actual Python3.14 native run_agent and conversation_loop imports passed as
UID65532 with read-only root, no network, dropped capabilities, private bounded
HOME/HERMES_HOME and CPU/RAM/PID limits. This image does not yet include the
adapter/entrypoint, and import success is not execution, continuation,
no-listener, containment or B5 acceptance evidence. The existing upstream
dashboard metadata advisory remains open pending reachable-surface proof.

## Narrow source-image evidence

Worker `sha256:8cbde14ca35ea7cf9977c77260d6fe8d95dfc275e2155a37151f88042c75a04d`
has its own 2026-10-03 scan and SPDX inventory, with zero HIGH/CRITICAL scan
findings. Independent actual-image tests verify that only Todo definitions are
advertised and direct or bridged hidden tools are rejected before checkpoint or
handler dispatch. Actual native SDK continuation/compaction tests and production
bootstrap tmpfs/hash/readiness checks pass for this exact image. This does not
establish full native research execution or crash recovery.

Server `sha256:435fde1c336c09fe2fcda69b6d81a1c69aa4f833a69e29a05e2583f8d87e21d3`
has a separate zero HIGH/CRITICAL scan and SPDX inventory. Actual private-service
HTTP with synthetic credentials, PostgreSQL and MinIO proves durable boundary
ACK replay, exact context/workspace restore, corruption rejection before
replacement, authorization and exact service stop. No paid provider was called.
Subsequent source images require fresh inventory, scan and behavioral evidence.

A dedicated 8cb worker also proves the kernel's hard memory limit: 1 GiB memory,
zero swap, OOMKilled=true and exit 137, followed by exact owned-engine cleanup.
Other resource/network evidence and the full application lifecycle remain
separate checks; this resource-only test overrides the application command.

`runtime/Dockerfile` and `runtime/prepare_build.py` provide a pinned, verified
source-context recipe; `backend/Dockerfile` uses a Dockerfile-specific context
allowlist so Hub, local state and secret files are excluded before context
transfer. Both require the reviewed dependency prerequisite images to exist.
Production prerequisite provisioning and storage/network composition remain in
the deployment tranche. B5, the native dashboard advisory surface control and
the retained MinIO Thrift HIGH finding must not be declared accepted from these
narrow checks alone.
