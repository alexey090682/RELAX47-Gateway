#!/usr/bin/with-contenv bashio
set -euo pipefail

router_host="$(bashio::config 'router_host')"
router_model="$(bashio::config 'router_model')"
router_firmware="$(bashio::config 'router_firmware')"
router_password="$(bashio::config 'router_password')"
lan_cidr="$(bashio::config 'lan_cidr')"
write_mode="$(bashio::config 'write_mode')"
confirmation_code="$(bashio::config 'confirmation_code')"
allowed_service_domains="$(bashio::config 'allowed_service_domains')"
auto_verify_writes="$(bashio::config 'auto_verify_writes')"
admin_all_service_domains="$(bashio::config 'admin_all_service_domains')"
secure_mcp_enabled="$(bashio::config 'secure_mcp_enabled')"
tunnel_id="$(bashio::config 'tunnel_id')"
tunnel_api_key="$(bashio::config 'tunnel_api_key')"
public_mcp_enabled="$(bashio::config 'public_mcp_enabled')"
public_mcp_port="$(bashio::config 'public_mcp_port')"
public_base_url="$(bashio::config 'public_base_url')"
oauth_activation_code="$(bashio::config 'oauth_activation_code')"
oauth_token_hours="$(bashio::config 'oauth_token_hours')"
public_rate_limit_per_minute="$(bashio::config 'public_rate_limit_per_minute')"
public_max_request_bytes="$(bashio::config 'public_max_request_bytes')"
public_session_hours="$(bashio::config 'public_session_hours')"
ssh_enabled="$(bashio::config 'ssh_enabled')"
ssh_port="$(bashio::config 'ssh_port')"
ssh_authorized_keys="$(bashio::config 'ssh_authorized_keys')"
reverse_ssh_enabled="$(bashio::config 'reverse_ssh_enabled')"
reverse_ssh_host="$(bashio::config 'reverse_ssh_host')"
reverse_ssh_port="$(bashio::config 'reverse_ssh_port')"
reverse_ssh_user="$(bashio::config 'reverse_ssh_user')"
reverse_ssh_remote_port="$(bashio::config 'reverse_ssh_remote_port')"
reverse_ssh_private_key="$(bashio::config 'reverse_ssh_private_key')"

export RELAX47_ROUTER_HOST="$router_host"
export RELAX47_ROUTER_MODEL="$router_model"
export RELAX47_ROUTER_FIRMWARE="$router_firmware"
export RELAX47_ROUTER_PASSWORD="$router_password"
export RELAX47_LAN_CIDR="$lan_cidr"
export RELAX47_WRITE_MODE="$write_mode"
export RELAX47_CONFIRMATION_CODE="$confirmation_code"
export RELAX47_ALLOWED_SERVICE_DOMAINS="$allowed_service_domains"
export RELAX47_AUTO_VERIFY_WRITES="$auto_verify_writes"
export RELAX47_ADMIN_ALL_SERVICE_DOMAINS="$admin_all_service_domains"
export RELAX47_PUBLIC_MCP_ENABLED="$public_mcp_enabled"
export RELAX47_PUBLIC_MCP_PORT="$public_mcp_port"
export RELAX47_PUBLIC_BASE_URL="$public_base_url"
export RELAX47_OAUTH_ACTIVATION_CODE="$oauth_activation_code"
export RELAX47_OAUTH_TOKEN_HOURS="$oauth_token_hours"
export RELAX47_PUBLIC_RATE_LIMIT_PER_MINUTE="$public_rate_limit_per_minute"
export RELAX47_PUBLIC_MAX_REQUEST_BYTES="$public_max_request_bytes"
export RELAX47_PUBLIC_SESSION_HOURS="$public_session_hours"

bashio::log.info "Starting RELAX47 Local Gateway (write_mode=${write_mode})"
python3 /opt/relax47/gateway.py &
gateway_pid=$!

if bashio::var.true "$public_mcp_enabled"; then
  if [[ ! "$public_base_url" =~ ^https://[^/]+$ ]]; then
    bashio::log.warning "Public MCP stays disabled: public_base_url must be an HTTPS origin without a trailing slash"
  elif [[ ${#oauth_activation_code} -lt 12 ]]; then
    bashio::log.warning "Public MCP stays disabled: oauth_activation_code must contain at least 12 characters"
  elif [[ ! -s /ssl/fullchain.pem || ! -s /ssl/privkey.pem ]]; then
    bashio::log.warning "Public MCP stays disabled: /ssl/fullchain.pem or /ssl/privkey.pem is missing"
  else
    python3 /opt/relax47/public_mcp.py &
    bashio::log.info "Public HTTPS MCP started on internal port ${public_mcp_port}; external endpoint ${public_base_url}/mcp"
  fi
fi

if bashio::var.true "$ssh_enabled" || bashio::var.true "$reverse_ssh_enabled"; then
  if [[ -z "$ssh_authorized_keys" ]]; then
    bashio::log.warning "SSH requested but no authorized key is configured; SSH stays disabled"
  else
    if ! id relax47 >/dev/null 2>&1; then
      adduser -D -h /data/ssh-user -s /bin/ash relax47
    fi
    mkdir -p /data/ssh /data/ssh-user/.ssh
    chmod 0700 /data/ssh /data/ssh-user/.ssh
    printf '%s\n' "$ssh_authorized_keys" > /data/ssh-user/.ssh/authorized_keys
    chmod 0600 /data/ssh-user/.ssh/authorized_keys
    chown -R relax47:relax47 /data/ssh-user
    if [[ ! -f /data/ssh/ssh_host_ed25519_key ]]; then
      ssh-keygen -q -t ed25519 -N '' -f /data/ssh/ssh_host_ed25519_key
    fi
    if [[ ! -f /data/ssh/ssh_host_rsa_key ]]; then
      ssh-keygen -q -t rsa -b 3072 -N '' -f /data/ssh/ssh_host_rsa_key
    fi
    cat > /data/sshd_config <<EOF
Port ${ssh_port}
ListenAddress 0.0.0.0
Protocol 2
HostKey /data/ssh/ssh_host_ed25519_key
HostKey /data/ssh/ssh_host_rsa_key
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile /data/ssh-user/.ssh/authorized_keys
AllowUsers relax47
AllowTcpForwarding no
X11Forwarding no
PermitTunnel no
GatewayPorts no
ClientAliveInterval 60
ClientAliveCountMax 3
Subsystem sftp internal-sftp
EOF
    /usr/sbin/sshd -D -e -f /data/sshd_config &
    bashio::log.info "Key-only SSH enabled on LAN port ${ssh_port}"
  fi
fi

if bashio::var.true "$reverse_ssh_enabled" && [[ -n "$ssh_authorized_keys" ]]; then
  if [[ -z "$reverse_ssh_host" || -z "$reverse_ssh_user" || -z "$reverse_ssh_private_key" ]]; then
    bashio::log.warning "Reverse SSH configuration is incomplete; channel stays disabled"
  else
    printf '%s\n' "$reverse_ssh_private_key" > /data/reverse_ssh_key
    chmod 0600 /data/reverse_ssh_key
    autossh -M 0 -N \
      -o BatchMode=yes \
      -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -o StrictHostKeyChecking=accept-new \
      -i /data/reverse_ssh_key \
      -p "$reverse_ssh_port" \
      -R "127.0.0.1:${reverse_ssh_remote_port}:127.0.0.1:${ssh_port}" \
      "${reverse_ssh_user}@${reverse_ssh_host}" &
    bashio::log.info "Reverse SSH channel requested"
  fi
fi

write_tunnel_state() {
  local state="$1"
  local detail="$2"
  jq -n \
    --arg state "$state" \
    --arg detail "$detail" \
    --arg checked_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{state: $state, detail: $detail, checked_at: $checked_at}' \
    > /run/relax47/tunnel_state.json
}

write_tunnel_state "disabled" "secure_mcp_enabled=false"

if bashio::var.true "$secure_mcp_enabled"; then
  if ! command -v tunnel-client >/dev/null 2>&1; then
    write_tunnel_state "unavailable" "tunnel-client is not installed"
    bashio::log.warning "Secure MCP tunnel client is not installed in this local build; the local MCP endpoint remains available"
  elif [[ ! "$tunnel_id" =~ ^tunnel_[0-9a-f]{32}$ || -z "$tunnel_api_key" ]]; then
    write_tunnel_state "configuration_error" "tunnel ID or runtime key is invalid"
    bashio::log.warning "Secure MCP is enabled but tunnel ID or runtime API key is invalid; tunnel stays disabled"
  else
    export CONTROL_PLANE_API_KEY="$tunnel_api_key"
    export HEALTH_LISTEN_ADDR="127.0.0.1:8080"
    tunnel_profile_dir="/data/tunnel-client-profiles"
    mkdir -p "$tunnel_profile_dir"
    chmod 0700 "$tunnel_profile_dir"
    export TUNNEL_CLIENT_PROFILE_DIR="$tunnel_profile_dir"
    write_tunnel_state "initializing" "creating a no-auth HTTP profile for the local MCP endpoint"
    if /usr/bin/tunnel-client init \
      --sample sample_mcp_remote_no_auth \
      --profile relax47 \
      --tunnel-id "$tunnel_id" \
      --mcp-server-url http://127.0.0.1:8765/mcp \
      --force; then
      write_tunnel_state "validating" "checking control-plane and local MCP configuration"
      /usr/bin/tunnel-client doctor --profile relax47 --explain || \
        bashio::log.warning "Secure MCP doctor reported a problem; the client will keep retrying"
      : > /data/tunnel-client.log
      chmod 0600 /data/tunnel-client.log
      /usr/bin/tunnel-client run --profile relax47 > /data/tunnel-client.log 2>&1 &
      tunnel_pid=$!
      write_tunnel_state "running" "tunnel-client process started; readiness pending"
      (
        while kill -0 "$tunnel_pid" 2>/dev/null; do
          if curl -fsS --max-time 3 http://127.0.0.1:8080/readyz >/dev/null 2>&1; then
            write_tunnel_state "ready" "outbound HTTPS tunnel is connected and MCP is ready"
          elif curl -fsS --max-time 3 http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
            write_tunnel_state "not_ready" "process is healthy but the control plane or MCP probe is not ready"
          else
            write_tunnel_state "starting" "waiting for tunnel-client health endpoint"
          fi
          sleep 10
        done
        write_tunnel_state "stopped" "tunnel-client process exited; inspect add-on log"
      ) &
      bashio::log.info "Secure MCP Tunnel started (outbound HTTPS only)"
    else
      write_tunnel_state "initialization_failed" "Secure MCP profile initialization failed"
      bashio::log.warning "Secure MCP profile initialization failed; tunnel stays disabled"
    fi
  fi
fi

wait "$gateway_pid"
