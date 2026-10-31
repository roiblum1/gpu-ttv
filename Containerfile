# GPU Time-to-Value image: ttv-engine (default command), ttv-collector and ttv-pricing (ADR-0007).
#   podman build -t gpu-ttv:dev .
# Air-gapped builds point PIP_INDEX_URL and the two base images at internal mirrors, or put a
# wheelhouse in offline/wheelhouse/ (offline/README.md). Nothing is downloaded at runtime.
# Base images are pinned by multi-arch index digest (python 3.12.16-slim-trixie, uv 0.13.6), as in
# offline/images.lock. No tag next to the digest: skopeo and some runtimes refuse both together.
ARG PYTHON_IMAGE=docker.io/library/python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
ARG UV_IMAGE=ghcr.io/astral-sh/uv@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 UV_LINK_MODE=copy
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Runtime dependencies only, every file checked against uv.lock's hashes, wheels only (no builds).
RUN --mount=type=bind,source=offline,target=/mnt/offline \
    uv export --quiet --frozen --no-dev --no-emit-project --format requirements-txt \
      > /tmp/requirements.txt \
 && uv venv --python /usr/local/bin/python3.12 /app/.venv \
 && if [ -d /mnt/offline/wheelhouse ]; then src="--no-index --find-links /mnt/offline/wheelhouse"; \
    else src="--index-url ${PIP_INDEX_URL}"; fi \
 && uv pip install --python /app/.venv/bin/python --require-hashes --no-deps \
      --only-binary :all: --compile-bytecode ${src} -r /tmp/requirements.txt
COPY src/ ./src/
# The project runs from /app/src, so store.MIGRATIONS finds /app/migrations (ADR-0007, "Image").
# A .pth file puts src on the path; the console scripts are generated from [project.scripts].
RUN /app/.venv/bin/python -c 'import pathlib, sysconfig, tomllib; \
site = pathlib.Path(sysconfig.get_paths()["purelib"]); \
(site / "gpu_ttv.pth").write_text("/app/src\n"); \
scripts = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["scripts"]; \
targets = {n: t.split(":") for n, t in scripts.items()}; \
paths = {n: pathlib.Path("/app/.venv/bin") / n for n in targets}; \
[paths[n].write_text(f"#!/app/.venv/bin/python\nimport sys\nfrom {m} import {f}\nsys.exit({f}())\n") \
 for n, (m, f) in targets.items()]; \
[p.chmod(0o755) for p in paths.values()]' \
 && /app/.venv/bin/python -m compileall -q --invalidation-mode unchecked-hash /app/src

FROM ${PYTHON_IMAGE}
ARG VERSION=0.1.0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="gpu-ttv" \
      org.opencontainers.image.description="GPU Time-to-Value: ttv-engine, ttv-collector, ttv-pricing" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="Proprietary"
# HOME=/tmp: OpenShift runs an arbitrary UID whose HOME (/) is not writable.
ENV PATH=/app/.venv/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp
WORKDIR /app
# Files stay root-owned and world-readable: any UID can run them and none can change them.
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
COPY migrations/ /app/migrations/
COPY config/ /app/config/
USER 1001
EXPOSE 8080
CMD ["ttv-engine"]
