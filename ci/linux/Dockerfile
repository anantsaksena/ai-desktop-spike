# Local reproduction of the CI Linux build (developer tool only, NOT shipped
# to users). Same base and same install script as .github/workflows/build.yml.
#   docker build -t aidesk-linux -f ci/linux/Dockerfile ci/linux
#   docker run --rm -v "$PWD":/src -w /src aidesk-linux ci/linux/build_all.sh
FROM ubuntu:22.04
COPY install_deps.sh /tmp/install_deps.sh
RUN bash /tmp/install_deps.sh && rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.cargo/bin:${PATH}" \
    APPIMAGE_EXTRACT_AND_RUN=1
