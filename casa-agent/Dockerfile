ARG BUILD_FROM
FROM ${BUILD_FROM}

# Base: ghcr.io/home-assistant/{arch}-base-debian:bookworm (glibc-based,
# ships s6-overlay + bashio). We migrated from alpine-python:3.12 in
# v0.8.1 because fastembed>=0.4 depends on onnxruntime, which has no
# musl wheels on PyPI.

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 python3-pip python3-venv \
         nodejs npm git nginx-light jq curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

# Plan 4a: Tier 1 baseline plugin packs for claude_code driver.
# These are symlinked into each engagement's isolated HOME at provisioning time.
# Updating the pin is a Casa image release concern; see CHANGELOG for cadence.
ARG SUPERPOWERS_REF=v5.0.7
RUN mkdir -p /opt/casa/claude-plugins/base \
    && git clone --depth=1 --branch ${SUPERPOWERS_REF} \
       https://github.com/obra/superpowers \
       /opt/casa/claude-plugins/base/superpowers \
    && rm -rf /opt/casa/claude-plugins/base/superpowers/.git

# Install ttyd (not packaged in Debian).
ARG TTYD_VERSION=1.7.7
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        amd64) TTYD_ARCH="x86_64" ;; \
        arm64) TTYD_ARCH="aarch64" ;; \
        *) echo "Unsupported arch: ${TARGETARCH}" && exit 1 ;; \
    esac \
    && curl -fsSL \
       "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${TTYD_ARCH}" \
       -o /usr/bin/ttyd \
    && chmod +x /usr/bin/ttyd

# Python dependencies live in a venv under /opt/casa/venv — Debian
# 12's system Python is PEP 668 "externally managed"; the venv keeps
# pip off the system site-packages without --break-system-packages.
# All runtime services invoke Python via /opt/casa/venv/bin/python3
# (put on PATH below).
RUN python3 -m venv /opt/casa/venv
ENV PATH="/opt/casa/venv/bin:${PATH}"

COPY requirements.txt /tmp/
RUN /opt/casa/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# Copy application
COPY rootfs /

# Set permissions for s6-rc services and scripts
RUN chmod +x /etc/s6-overlay/scripts/*.sh \
    && find /etc/s6-overlay/s6-rc.d -name 'run' -o -name 'finish' \
    | xargs chmod +x \
    && chmod +x /opt/casa/scripts/*.sh
