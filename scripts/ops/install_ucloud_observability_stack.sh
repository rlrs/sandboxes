#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: install_ucloud_observability_stack.sh \
  --private-bind-ip <ipv4> \
  [--endpoint-host <host>] \
  [--data-root <absolute-path>] \
  [--public-url https://<host>/grafana/] \
  [--gateway-url http://<host>:<port>] \
  [--gateway-token-file <path>]

Installs a bounded single-gateway observability stack for UCloud. OTLP/HTTP is
exposed only on the supplied private IP, or with 0.0.0.0 on every interface of
a gateway whose only non-loopback network is the private one (UCloud VMs: the
private address changes when the VM restarts; workers then reach the collector
at --endpoint-host, the gateway's DNS name). Grafana, Tempo, and
VictoriaMetrics query ports remain loopback-only. With --public-url, Grafana
serves under that sub-path for a TLS proxy in front of it (anonymous access is
always off). VictoriaMetrics scrapes the gateway's fleet metrics
(/v1/metrics/prometheus at --gateway-url, default http://<endpoint-host>:8090)
with the gateway token, which Grafana's gateway data source also reads; both
read it from a root-owned copy, never from a dashboard.
EOF
}

private_bind_ip=""
endpoint_host=""
data_root=/var/lib/ucloud-sandboxes/telemetry
public_url=""
gateway_url=""
gateway_token_file=/var/lib/ucloud-sandboxes/state/gateway-token
while (($#)); do
  case "$1" in
    --private-bind-ip) private_bind_ip="${2:-}"; shift 2 ;;
    --endpoint-host) endpoint_host="${2:-}"; shift 2 ;;
    --data-root) data_root="${2:-}"; shift 2 ;;
    --public-url) public_url="${2:-}"; shift 2 ;;
    --gateway-url) gateway_url="${2:-}"; shift 2 ;;
    --gateway-token-file) gateway_token_file="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  echo "UCloud observability installation must run as root" >&2
  exit 2
fi
if [[ ! "$private_bind_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "--private-bind-ip must be an IPv4 address" >&2
  exit 2
fi
if [[ "$private_bind_ip" != 0.0.0.0 ]] && ! ip -4 address show | grep -Fq " $private_bind_ip/"; then
  echo "--private-bind-ip is not assigned to this gateway" >&2
  exit 2
fi
endpoint_host="${endpoint_host:-$private_bind_ip}"
if [[ "$endpoint_host" == 0.0.0.0 || ! "$endpoint_host" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "--endpoint-host must name this gateway when binding 0.0.0.0" >&2
  exit 2
fi
if [[ "$data_root" != /* ]]; then
  echo "--data-root must be absolute" >&2
  exit 2
fi
case "$data_root/" in
  /work/*|/mnt/ucloud/*)
    echo "--data-root must use gateway-local storage, not shared UCloud storage" >&2
    exit 2
    ;;
esac

gateway_url="${gateway_url:-http://$endpoint_host:8090}"
if [[ ! "$gateway_url" =~ ^http://[A-Za-z0-9.-]+:[0-9]+$ ]]; then
  echo "--gateway-url must be http://<host>:<port>" >&2
  exit 2
fi
if [[ -n "$public_url" && ! "$public_url" =~ ^https://[A-Za-z0-9.-]+/[A-Za-z0-9._-]+/$ ]]; then
  echo "--public-url must be https://<host>/<sub-path>/" >&2
  exit 2
fi
if [[ ! -s "$gateway_token_file" ]]; then
  echo "--gateway-token-file must name the gateway token" >&2
  exit 2
fi
grafana_domain=127.0.0.1
grafana_public_env=""
grafana_health=http://127.0.0.1:3000/api/health
if [[ -n "$public_url" ]]; then
  grafana_domain="${public_url#https://}"
  grafana_domain="${grafana_domain%%/*}"
  grafana_public_env="-e GF_SERVER_ROOT_URL=$public_url -e GF_SERVER_SERVE_FROM_SUB_PATH=true -e GF_SECURITY_COOKIE_SECURE=true -e GF_SECURITY_COOKIE_SAMESITE=strict"
  grafana_health="http://127.0.0.1:3000/${public_url#https://*/}api/health"
fi

config_root=/etc/ucloud-sandboxes/telemetry
grafana_provisioning="$config_root/grafana/provisioning"
grafana_env="$config_root/grafana.env"
installer_dir="$(cd "$(dirname "$0")" && pwd)"
report_source="$installer_dir/ucloud_observability_report.py"

install -d -m 0755 \
  "$config_root" \
  "$grafana_provisioning/datasources" \
  "$grafana_provisioning/dashboards" \
  "$config_root/grafana/dashboards" \
  "$data_root/tempo" \
  "$data_root/victoria-metrics" \
  "$data_root/grafana"
chmod 0700 "$data_root/tempo" "$data_root/victoria-metrics"
chown -R 472:0 "$data_root/grafana"
chmod 0750 "$data_root/grafana"
# The scraper runs as root and Grafana as uid 472; each gets its own copy.
install -m 0400 -o root -g root "$gateway_token_file" "$config_root/gateway-token"
install -m 0400 "$gateway_token_file" "$config_root/grafana/gateway-token"
chown 472:0 "$config_root/grafana/gateway-token"
cat >"$config_root/scrape.yaml" <<EOF
scrape_configs:
  - job_name: ucloud-gateway
    scrape_interval: 15s
    scrape_timeout: 10s
    metrics_path: /v1/metrics/prometheus
    bearer_token_file: /etc/victoria/gateway-token
    static_configs:
      - targets: ["${gateway_url#http://}"]
EOF

cat >"$config_root/tempo.yaml" <<'EOF'
stream_over_http_enabled: true
server:
  http_listen_port: 3200
  grpc_listen_port: 9095
distributor:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318
ingester:
  max_block_duration: 5m
  complete_block_timeout: 10m
  flush_all_on_shutdown: true
compactor:
  compaction:
    block_retention: 72h
storage:
  trace:
    backend: local
    wal:
      path: /var/tempo/wal
    local:
      path: /var/tempo/blocks
usage_report:
  reporting_enabled: false
EOF

cat >"$config_root/collector.yaml" <<'EOF'
extensions:
  health_check:
    endpoint: 0.0.0.0:13133
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 192
    spike_limit_mib: 48
  batch:
    send_batch_size: 512
    timeout: 2s
exporters:
  otlp_grpc/tempo:
    endpoint: ucloud-telemetry-tempo:4317
    tls:
      insecure: true
    sending_queue:
      enabled: true
      queue_size: 2048
    retry_on_failure:
      enabled: true
      max_elapsed_time: 60s
  prometheusremotewrite/victoria:
    endpoint: http://ucloud-telemetry-victoria:8428/api/v1/write
    resource_to_telemetry_conversion:
      enabled: true
    remote_write_queue:
      enabled: true
      queue_size: 2048
    max_batch_request_parallelism: 1
service:
  extensions: [health_check]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [otlp_grpc/tempo]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [prometheusremotewrite/victoria]
  telemetry:
    metrics:
      level: normal
EOF

cat >"$grafana_provisioning/datasources/ucloud.yaml" <<EOF
apiVersion: 1
datasources:
  - name: UCloud Metrics
    uid: ucloud-metrics
    type: prometheus
    access: proxy
    url: http://ucloud-telemetry-victoria:8428
    isDefault: true
    editable: false
    jsonData:
      httpMethod: POST
      timeInterval: 5s
  - name: UCloud Traces
    uid: ucloud-traces
    type: tempo
    access: proxy
    url: http://ucloud-telemetry-tempo:3200
    editable: false
    jsonData:
      tracesToMetrics:
        datasourceUid: ucloud-metrics
      serviceMap:
        datasourceUid: ucloud-metrics
      nodeGraph:
        enabled: true
  # Live lists (queued creates, builds) straight from the gateway's API.
  - name: Gateway
    uid: ucloud-gateway
    type: yesoreyeram-infinity-datasource
    access: proxy
    url: $gateway_url
    editable: false
    jsonData:
      allowedHosts: ["$gateway_url"]
      httpHeaderName1: Authorization
    secureJsonData:
      httpHeaderValue1: "Bearer \$__file{/etc/grafana/gateway-token}"
EOF

cat >"$grafana_provisioning/dashboards/ucloud.yaml" <<'EOF'
apiVersion: 1
providers:
  - name: UCloud
    orgId: 1
    folder: UCloud
    type: file
    disableDeletion: true
    updateIntervalSeconds: 30
    allowUiUpdates: false
    options:
      path: /var/lib/grafana/dashboards
EOF

# Dashboards are files beside this installer (scripts/ops/grafana/dashboards).
for dashboard in "$installer_dir"/grafana/dashboards/*.json; do
  install -m 0644 "$dashboard" "$config_root/grafana/dashboards/"
done

if [[ ! -s "$grafana_env" ]]; then
  umask 077
  {
    printf 'GF_SECURITY_ADMIN_USER=admin\n'
    printf 'GF_SECURITY_ADMIN_PASSWORD=%s\n' "$(openssl rand -hex 24)"
    printf 'GF_SECURITY_SECRET_KEY=%s\n' "$(openssl rand -hex 32)"
  } >"$grafana_env"
fi
chmod 0600 "$grafana_env"
if [[ -f "$report_source" ]]; then
  install -m 0755 "$report_source" /usr/local/bin/ucloud-observability-report
else
  echo "warning: ucloud_observability_report.py was not beside the installer" >&2
fi

install -d -m 0755 /etc/systemd/system
cat >/etc/systemd/system/ucloud-telemetry-tempo.service <<EOF
[Unit]
Description=UCloud Tempo trace store
After=docker.service network-online.target
Wants=docker.service network-online.target
RequiresMountsFor=$data_root

[Service]
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker network create ucloud-telemetry
ExecStartPre=-/usr/bin/docker rm -f ucloud-telemetry-tempo
ExecStart=/usr/bin/docker run --name ucloud-telemetry-tempo --network ucloud-telemetry --memory 768m --cpus 0.60 --cpu-shares 256 --user 0:0 --log-driver journald -p 127.0.0.1:3200:3200 -v $config_root/tempo.yaml:/etc/tempo.yaml:ro -v $data_root/tempo:/var/tempo grafana/tempo:2.10.7 -config.file=/etc/tempo.yaml
ExecStop=/usr/bin/docker stop --time 30 ucloud-telemetry-tempo
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/ucloud-telemetry-victoria.service <<EOF
[Unit]
Description=UCloud VictoriaMetrics store
After=docker.service network-online.target
Wants=docker.service network-online.target
RequiresMountsFor=$data_root

[Service]
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker network create ucloud-telemetry
ExecStartPre=-/usr/bin/docker rm -f ucloud-telemetry-victoria
ExecStart=/usr/bin/docker run --name ucloud-telemetry-victoria --network ucloud-telemetry --memory 384m --cpus 0.40 --cpu-shares 256 --user 0:0 --log-driver journald -p 127.0.0.1:8428:8428 -v $data_root/victoria-metrics:/victoria-metrics-data -v $config_root/scrape.yaml:/etc/victoria/scrape.yaml:ro -v $config_root/gateway-token:/etc/victoria/gateway-token:ro victoriametrics/victoria-metrics:v1.148.0 -promscrape.config=/etc/victoria/scrape.yaml -storageDataPath=/victoria-metrics-data -retentionPeriod=14d -storage.minFreeDiskSpaceBytes=20GB -memory.allowedPercent=50 -selfScrapeInterval=10s
ExecStop=/usr/bin/docker stop --time 30 ucloud-telemetry-victoria
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/ucloud-telemetry-collector.service <<EOF
[Unit]
Description=UCloud OpenTelemetry Collector
After=docker.service network-online.target ucloud-telemetry-tempo.service ucloud-telemetry-victoria.service
Wants=docker.service network-online.target
Requires=ucloud-telemetry-tempo.service ucloud-telemetry-victoria.service

[Service]
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker network create ucloud-telemetry
ExecStartPre=-/usr/bin/docker rm -f ucloud-telemetry-collector
ExecStart=/usr/bin/docker run --name ucloud-telemetry-collector --network ucloud-telemetry --memory 320m --cpus 0.50 --cpu-shares 256 --log-driver journald -p $private_bind_ip:4318:4318 -p 127.0.0.1:13133:13133 -v $config_root/collector.yaml:/etc/otelcol-contrib/config.yaml:ro otel/opentelemetry-collector-contrib:0.153.0 --config=/etc/otelcol-contrib/config.yaml
ExecStop=/usr/bin/docker stop --time 20 ucloud-telemetry-collector
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/ucloud-telemetry-grafana.service <<EOF
[Unit]
Description=UCloud Grafana observability UI
After=docker.service network-online.target ucloud-telemetry-tempo.service ucloud-telemetry-victoria.service
Wants=docker.service network-online.target
Requires=ucloud-telemetry-tempo.service ucloud-telemetry-victoria.service
RequiresMountsFor=$data_root

[Service]
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker network create ucloud-telemetry
ExecStartPre=-/usr/bin/docker rm -f ucloud-telemetry-grafana
ExecStart=/usr/bin/docker run --name ucloud-telemetry-grafana --network ucloud-telemetry --memory 1536m --cpus 1.0 --cpu-shares 256 --log-driver journald --env-file $grafana_env -e GF_AUTH_ANONYMOUS_ENABLED=false -e GF_USERS_ALLOW_SIGN_UP=false -e GF_ANALYTICS_REPORTING_ENABLED=false -e GF_ANALYTICS_CHECK_FOR_UPDATES=false -e GF_PLUGINS_PREINSTALL_SYNC=yesoreyeram-infinity-datasource@4.1.1 -e GF_PLUGINS_PREINSTALL_AUTO_UPDATE=false -e GF_SERVER_DOMAIN=$grafana_domain $grafana_public_env -p 127.0.0.1:3000:3000 -v $data_root/grafana:/var/lib/grafana -v $grafana_provisioning:/etc/grafana/provisioning:ro -v $config_root/grafana/dashboards:/var/lib/grafana/dashboards:ro -v $config_root/grafana/gateway-token:/etc/grafana/gateway-token:ro grafana/grafana:13.2.4
ExecStop=/usr/bin/docker stop --time 20 ucloud-telemetry-grafana
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

docker pull grafana/tempo:2.10.7
docker pull victoriametrics/victoria-metrics:v1.148.0
docker pull otel/opentelemetry-collector-contrib:0.153.0
docker pull grafana/grafana:13.2.4
systemctl daemon-reload
systemctl enable \
  ucloud-telemetry-tempo.service \
  ucloud-telemetry-victoria.service \
  ucloud-telemetry-collector.service \
  ucloud-telemetry-grafana.service
systemctl restart \
  ucloud-telemetry-tempo.service \
  ucloud-telemetry-victoria.service
systemctl restart \
  ucloud-telemetry-collector.service \
  ucloud-telemetry-grafana.service

for url in \
  http://127.0.0.1:3200/ready \
  http://127.0.0.1:8428/-/healthy \
  http://127.0.0.1:13133/ \
  "$grafana_health"; do
  for attempt in $(seq 1 90); do
    if curl -fsS "$url" >/dev/null; then break; fi
    if [[ "$attempt" == 90 ]]; then
      systemctl --no-pager --full status \
        ucloud-telemetry-tempo.service \
        ucloud-telemetry-victoria.service \
        ucloud-telemetry-collector.service \
        ucloud-telemetry-grafana.service
      exit 1
    fi
    sleep 1
  done
done

printf 'otlp_endpoint=http://%s:4318\n' "$endpoint_host"
printf 'grafana_url=%s\n' "${public_url:-http://127.0.0.1:3000/ (ssh -L 3000:127.0.0.1:3000)}"
printf 'grafana_admin_credentials=%s\n' "$grafana_env"
printf 'tempo_retention=72h\nmetrics_retention=14d\n'
