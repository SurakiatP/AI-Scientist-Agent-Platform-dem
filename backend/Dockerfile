# The reviewed prerequisite supplies Python 3.13.14 and locked MCP/A2A dependencies.
ARG DEPENDENCY_IMAGE=scientist-server-protocol-prerequisite@sha256:65f1c54e8407793b4da6a7523bf76d64963f2046eb9614ea74d11a67a0bad9ca
FROM ${DEPENDENCY_IMAGE}
COPY --chown=65532:65532 backend/src/ /opt/scientist/backend/src/
COPY --chown=65532:65532 backend/migrations/ /opt/scientist/backend/migrations/
COPY --chown=65532:65532 runtime/ /opt/scientist/runtime/
COPY --chown=65532:65532 docs/skills/capability-registry.json /opt/scientist/runtime/capability-registry.json
COPY --chown=65532:65532 skills/ /opt/scientist/skills/
COPY --chown=65532:65532 LICENSE.md /opt/scientist/notices/scientific-agent-skills-LICENSE.md
ENV PYTHONPATH=/opt/scientist/backend/src:/opt/scientist PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/scientist
USER 65532:65532
ENTRYPOINT ["/opt/python/bin/python3.13"]
