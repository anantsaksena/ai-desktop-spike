# Local reproduction of the CI Linux build (developer tool only, NOT shipped
# to users). Same base and same install script as .github/workflows/build.yml.
# Build context is the repo root (install_deps.sh uses scripts/lib/):
#   docker build -t aidesk-linux -f ci/linux/Dockerfile .
#   docker run --rm -v "$PWD":/src:ro -v aidesk-work:/work -e ISOLATE=1 -w /src aidesk-linux bash ci/linux/build_all.sh
# Base image pinned by multi-arch index digest (ubuntu:22.04 as of 2026-10-09).
FROM ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401
COPY scripts/lib/fetch_verified.sh /tmp/deps/scripts/lib/fetch_verified.sh
COPY ci/linux/install_deps.sh /tmp/deps/ci/linux/install_deps.sh
COPY rust-toolchain.toml /tmp/deps/rust-toolchain.toml
RUN bash /tmp/deps/ci/linux/install_deps.sh && rm -rf /var/lib/apt/lists/* /tmp/deps
ENV PATH="/root/.cargo/bin:/opt/node-v24.21.0/bin:${PATH}" \
    APPIMAGE_EXTRACT_AND_RUN=1
