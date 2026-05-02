ARG BUILD_FROM
FROM ${BUILD_FROM}

# Base: ghcr.io/home-assistant/{arch}-base-debian:bookworm (glibc-based,
# ships s6-overlay + bashio). We migrated from alpine-python:3.12 in
# v0.8.1 because fastembed>=0.4 depends on onnxruntime, which has no
# musl wheels on PyPI.

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 python3-pip python3-venv \
         nodejs npm git nginx-light jq yq curl ca-certificates unzip \
         openssh-client \
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

# gh CLI for plugin-developer's GitHub repo creation + push (§6.1 / §6.3 / §8.5)
ARG GH_CLI_VERSION=2.45.0
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) GH_ARCH="linux_amd64" ;; \
        arm64) GH_ARCH="linux_arm64" ;; \
        *) echo "unsupported arch $(dpkg --print-architecture)"; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_${GH_ARCH}.tar.gz"; \
    tar -xzf /tmp/gh.tar.gz -C /tmp; \
    mv /tmp/gh_${GH_CLI_VERSION}_${GH_ARCH}/bin/gh /usr/local/bin/; \
    rm -rf /tmp/gh*; \
    gh --version

# 1Password op CLI for secret resolution (§8 universal resolver)
ARG OP_CLI_VERSION=2.30.0
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) OP_ARCH="amd64" ;; \
        arm64) OP_ARCH="arm64" ;; \
        *) echo "unsupported arch $(dpkg --print-architecture)"; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/op.zip \
      "https://cache.agilebits.com/dist/1P/op2/pkg/v${OP_CLI_VERSION}/op_linux_${OP_ARCH}_v${OP_CLI_VERSION}.zip"; \
    unzip /tmp/op.zip -d /usr/local/bin; \
    rm /tmp/op.zip; \
    op --version

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
    && chmod +x /opt/casa/scripts/*.sh \
    && chmod +x /usr/local/bin/casactl

# --- Seed-marketplace build (§3.2 + §10.1 + spike §Gate 3) -------------------

# Copy marketplace catalogs into the image.
COPY rootfs/opt/casa/defaults/marketplace-defaults/ /opt/casa/defaults/marketplace-defaults/
COPY rootfs/opt/casa/defaults/marketplace-user/ /opt/casa/defaults/marketplace-user/

# Pin the default-marketplace plugin refs. Each entry also appears in
# /opt/casa/defaults/marketplace-defaults/.claude-plugin/marketplace.json
# with a matching `source:{source:"github",repo:"...",ref:"..."}` stanza so
# the seed is reproducible and offline-resolvable.
ARG SUPERPOWERS_REF=v5.0.7

# Create the non-root user the build-install runs under.
# `--allow-dangerously-skip-permissions` refuses to run as root.
RUN id -u casa >/dev/null 2>&1 || useradd -m -s /bin/bash casa; \
    mkdir -p /opt/claude-seed && chown -R casa:casa /opt/claude-seed

USER casa

# Build-time SSH→HTTPS rewrite is handled by /etc/gitconfig (shipped via
# `COPY rootfs /` above). System-level config applies to every USER, so
# the `casa` build user inherits the rewrite without any per-user setup.

# Build-time seed install. CLAUDE_CODE_PLUGIN_CACHE_DIR points at the seed's
# plugins/ root (parent of cache/) — NOT plugins/cache/. Per spike §Gate 1.
RUN CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed \
    claude plugin marketplace add /opt/casa/defaults/marketplace-defaults/ \
 && CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed \
    claude plugin install superpowers@casa-plugins-defaults \
 && CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed \
    claude plugin install plugin-dev@casa-plugins-defaults \
 && CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed \
    claude plugin install skill-creator@casa-plugins-defaults \
 && CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed \
    claude plugin install mcp-server-dev@casa-plugins-defaults \
 && CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed \
    claude plugin install document-skills@casa-plugins-defaults

USER root

# Spike §Gate 3 addendum: the build-time install populates
# known_marketplaces.json + cache/ but does NOT populate marketplaces/<name>/.
# Manually copy so runtime plugin resolution works.
RUN mkdir -p /opt/claude-seed/marketplaces \
 && cp -r /opt/casa/defaults/marketplace-defaults \
          /opt/claude-seed/marketplaces/casa-plugins-defaults \
 && chmod -R a-w /opt/claude-seed \
 && chmod -R a+rX /opt/claude-seed

# Make the seed visible to every consumer (casa-main + every engagement
# subprocess). Engagement subprocesses also get CLAUDE_CODE_PLUGIN_CACHE_DIR
# exported at spawn time (see §B workspace-template + §5.3).
ENV CLAUDE_CODE_PLUGIN_SEED_DIR=/opt/claude-seed
