# The reviewed prerequisite supplies Python 3.13.14 and uv.lock dependencies.
ARG DEPENDENCY_IMAGE=scientist-server-prerequisite@sha256:4912c149defb100b6c48c2d2674d98f0cd52183b1727ad26fd6fe3d260b696af
FROM ${DEPENDENCY_IMAGE}
COPY --chown=65532:65532 backend/src/ /opt/scientist/backend/src/
COPY --chown=65532:65532 backend/migrations/ /opt/scientist/backend/migrations/
COPY --chown=65532:65532 runtime/ /opt/scientist/runtime/
ENV PYTHONPATH=/opt/scientist/backend/src:/opt/scientist PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/scientist
USER 65532:65532
ENTRYPOINT ["/opt/python/bin/python3.13"]
