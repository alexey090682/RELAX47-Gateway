ARG BUILD_VERSION
ARG BUILD_ARCH

FROM ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.22
ARG BUILD_VERSION
ARG BUILD_ARCH

RUN apk add --no-cache \
    autossh \
    curl \
    iproute2 \
    iputils \
    jq \
    netcat-openbsd \
    openssh \
    procps-ng \
    ripgrep \
    unzip

RUN pip3 install --no-cache-dir \
    "cryptography==45.0.6" \
    "python-xiaomi-miwifi==0.6.0" \
    "tuya-device-sharing-sdk==0.2.14"

RUN set -eu; \
    release_base="https://github.com/openai/tunnel-client/releases/download/v0.0.11"; \
    asset="tunnel-client-v0.0.11-linux-amd64.zip"; \
    curl -fsSL "${release_base}/${asset}" -o /tmp/tunnel-client.zip; \
    curl -fsSL "${release_base}/SHA256SUMS.txt" -o /tmp/SHA256SUMS.txt; \
    expected="$(awk -v name="${asset}" '$2 == name || $2 == "*" name {print $1; exit}' /tmp/SHA256SUMS.txt)"; \
    test -n "${expected}"; \
    printf '%s  %s\n' "${expected}" '/tmp/tunnel-client.zip' | sha256sum -c -; \
    mkdir -p /tmp/tunnel-client-release; \
    unzip -q /tmp/tunnel-client.zip -d /tmp/tunnel-client-release; \
    tunnel_binary="$(find /tmp/tunnel-client-release -type f -name tunnel-client | head -n 1)"; \
    test -n "${tunnel_binary}"; \
    cp "${tunnel_binary}" /usr/bin/tunnel-client; \
    chmod 0755 /usr/bin/tunnel-client; \
    rm -rf /tmp/tunnel-client.zip /tmp/SHA256SUMS.txt /tmp/tunnel-client-release

COPY gateway.py /opt/relax47/gateway.py
COPY localtuya_migration.py /opt/relax47/localtuya_migration.py
COPY maintenance.py /opt/relax47/maintenance.py
COPY sql_migration.py /opt/relax47/sql_migration.py
COPY runtime_migration.py runtime_cutover.py ha_sql_store.py backup_retention.py /opt/relax47/
COPY sql_schemas /opt/relax47/sql_schemas
COPY relax47_pytuya.py /opt/relax47/relax47_pytuya.py
COPY public_mcp.py /opt/relax47/public_mcp.py
COPY run.sh /run.sh

RUN chmod 0755 /run.sh /opt/relax47/gateway.py /opt/relax47/public_mcp.py \
        /opt/relax47/localtuya_migration.py /opt/relax47/maintenance.py \
    && mkdir -p /data /share /homeassistant /run/relax47

CMD ["/run.sh"]

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"
