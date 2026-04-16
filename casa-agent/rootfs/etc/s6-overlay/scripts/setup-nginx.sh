#!/command/with-contenv bashio

INGRESS_PORT=$(bashio::addon.ingress_port)

cat > /etc/nginx/nginx.conf <<NGINX
worker_processes 1;
error_log /dev/stdout info;
pid /tmp/nginx.pid;

events { worker_connections 128; }

http {
    map \$http_upgrade \$connection_upgrade {
        default upgrade;
        ''      close;
    }

    # --- Ingress server (HA-authenticated) ---
    server {
        listen ${INGRESS_PORT} default_server;
        server_name _;

        location / {
            proxy_pass http://127.0.0.1:8099;
            proxy_http_version 1.1;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Ingress-Path \$http_x_ingress_path;
            proxy_read_timeout 300;
        }

        location /ws {
            proxy_pass http://127.0.0.1:8099/ws;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_read_timeout 86400;
        }
NGINX

# Terminal on ingress
if bashio::config.true 'enable_terminal'; then
    cat >> /etc/nginx/nginx.conf <<NGINX
        location /terminal/ {
            proxy_pass http://127.0.0.1:7681/terminal/;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_read_timeout 86400;
            proxy_send_timeout 86400;
        }
NGINX
else
    cat >> /etc/nginx/nginx.conf <<'NGINX'
        location /terminal/ {
            default_type text/html;
            return 200 '<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Casa Terminal</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; display: flex;
         justify-content: center; align-items: center; min-height: 80vh;
         margin: 0; background: #1e293b; color: #e2e8f0; }
  .card { text-align: center; max-width: 420px; padding: 2rem; }
  h1 { font-size: 1.4rem; margin-bottom: 0.5rem; }
  p { color: #94a3b8; line-height: 1.6; }
  code { background: #334155; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
</style></head>
<body><div class="card">
  <h1>Web Terminal is disabled</h1>
  <p>To enable it, go to <strong>Settings &rarr; Add-ons &rarr; Casa Agent &rarr; Configuration</strong>
     and set <code>Enable Web Terminal</code> to on, then restart the add-on.</p>
</div></body></html>';
        }
NGINX
fi

# Close ingress server block
cat >> /etc/nginx/nginx.conf <<'NGINX'
    }

    # --- External API server (no terminal) ---
    server {
        listen 18065;
        server_name _;

        location / {
            proxy_pass http://127.0.0.1:8099;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_read_timeout 300;
        }

        location /ws {
            proxy_pass http://127.0.0.1:8099/ws;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_read_timeout 86400;
        }

        location /terminal/ {
            return 404;
        }
    }
}
NGINX

bashio::log.info "Nginx configured (terminal: $(bashio::config 'enable_terminal'))"
