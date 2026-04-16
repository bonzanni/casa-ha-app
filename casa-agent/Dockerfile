ARG BUILD_FROM
FROM ${BUILD_FROM}

# Base: ghcr.io/home-assistant/{arch}-base-python:3.12-alpine3.22
# Node.js required by Claude Agent SDK (bundles Claude Code CLI)
RUN apk add --no-cache nodejs git nginx jq curl

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

# Set permissions for s6 services and init scripts
RUN chmod +x /etc/cont-init.d/*.sh \
    && chmod +x /etc/services.d/*/run \
    && chmod +x /etc/services.d/*/finish \
    && chmod +x /opt/casa/scripts/*.sh
