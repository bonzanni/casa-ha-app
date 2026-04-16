ARG BUILD_FROM
FROM ${BUILD_FROM}

# Base: ghcr.io/home-assistant/{arch}-base-python:3.12-alpine3.22
# Node.js + npm required by Claude Agent SDK (wraps the Claude Code CLI)
RUN apk add --no-cache nodejs npm git nginx jq curl \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

# Install ttyd (not packaged in Alpine)
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

# Install Python dependencies
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Copy application
COPY rootfs /

# Set permissions for s6-rc services and scripts
RUN chmod +x /etc/s6-overlay/scripts/*.sh \
    && find /etc/s6-overlay/s6-rc.d -name 'run' -o -name 'finish' \
    | xargs chmod +x \
    && chmod +x /opt/casa/scripts/*.sh
