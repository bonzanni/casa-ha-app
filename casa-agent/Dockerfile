# Multi-arch Debian base (glibc, ships s6-overlay + bashio). The default keeps
# Supervisor local builds working now that build.yaml is retired upstream and
# Supervisor ≥2026.04 no longer injects BUILD_FROM; under buildx the same
# manifest resolves to the right per-platform layer. We migrated off
# alpine-python:3.12 in v0.8.1 for glibc wheels (onnxruntime, since retired);
# the glibc base is kept.
ARG BUILD_FROM=ghcr.io/home-assistant/base-debian:bookworm
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 python3-pip python3-venv \
         nodejs npm git nginx-light jq yq curl ca-certificates unzip \
         openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@2.1.150 \
    && npm cache clean --force

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

# --- Seed-marketplace build (§3.2 + §10.1 + spike §Gate 3) -------------------
# L31: this network-bound seed install runs BEFORE `COPY rootfs /` so a routine
# code change does not invalidate its layer cache (it only re-runs when the
# marketplace catalogs / gitconfig / credential helper below change). Its true
# inputs are narrow-COPY'd here; the later `COPY rootfs /` re-copies them with
# identical content (harmless).

# Build-time SSH→HTTPS rewrite is handled by /etc/gitconfig; ship it (and the
# credential helper it references) via narrow COPYs so the seed does not depend
# on the broad `COPY rootfs /` below. System-level gitconfig applies to every
# USER, so the `casa` build user inherits the rewrite without per-user setup.
COPY rootfs/etc/gitconfig /etc/gitconfig
COPY rootfs/opt/casa/scripts/git-credential-casa.sh /opt/casa/scripts/git-credential-casa.sh

# Copy marketplace catalogs into the image.
COPY rootfs/opt/casa/defaults/marketplace-defaults/ /opt/casa/defaults/marketplace-defaults/
COPY rootfs/opt/casa/defaults/marketplace-user/ /opt/casa/defaults/marketplace-user/

# Plugin refs are pinned solely in
# /opt/casa/defaults/marketplace-defaults/.claude-plugin/marketplace.json
# (superpowers v5.1.0 as of v0.46.3); the seed install below resolves refs
# from that file — there is no ARG pin to keep in sync.

# Create the non-root user the build-install runs under.
# `--allow-dangerously-skip-permissions` refuses to run as root.
RUN id -u casa >/dev/null 2>&1 || useradd -m -s /bin/bash casa; \
    mkdir -p /opt/claude-seed && chown -R casa:casa /opt/claude-seed

USER casa

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
    claude plugin install context7@casa-plugins-defaults

USER root

# Spike §Gate 3 addendum: the build-time install populates
# known_marketplaces.json + cache/ but does NOT populate marketplaces/<name>/.
# Manually copy so runtime plugin resolution works.
RUN mkdir -p /opt/claude-seed/marketplaces \
 && cp -r /opt/casa/defaults/marketplace-defaults \
          /opt/claude-seed/marketplaces/casa-plugins-defaults \
 && chmod -R a-w /opt/claude-seed \
 && chmod -R a+rX /opt/claude-seed

# Copy application (frequently-changing layer; kept AFTER the seed install so a
# code edit does not bust the seed-install layer cache — L31).
COPY rootfs /

# Set permissions for s6-rc services and scripts
RUN chmod +x /etc/s6-overlay/scripts/*.sh \
    && find /etc/s6-overlay/s6-rc.d -name 'run' -o -name 'finish' \
    | xargs chmod +x \
    && chmod +x /opt/casa/scripts/*.sh \
    && chmod +x /usr/local/bin/casactl

# Make the seed visible to every consumer (casa-main + every engagement
# subprocess). Engagement subprocesses also get CLAUDE_CODE_PLUGIN_CACHE_DIR
# exported at spawn time (see §B workspace-template + §5.3).
ENV CLAUDE_CODE_PLUGIN_SEED_DIR=/opt/claude-seed

# io.hass.* labels are injected by the deploy workflow (build-image action).
LABEL org.opencontainers.image.title="Casa Agent" \
      org.opencontainers.image.description="Personal AI agent framework powered by Claude" \
      org.opencontainers.image.source="https://github.com/bonzanni/casa-ha-app" \
      org.opencontainers.image.licenses="MIT"
