#!/bin/sh
# Test override: generates nginx config with hardcoded port instead of bashio
OPTIONS=/data/options.json
INGRESS_PORT=8080
TERMINAL_ENABLED=$(jq -r '.enable_terminal // false' "$OPTIONS")

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

    server {
        listen ${INGRESS_PORT} default_server;
        server_name _;

        location / {
            proxy_pass http://127.0.0.1:8099;
            proxy_http_version 1.1;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
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

if [ "$TERMINAL_ENABLED" = "true" ]; then
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
fi

cat >> /etc/nginx/nginx.conf <<NGINX
    }
}
NGINX

echo "[INFO] Nginx configured (terminal: ${TERMINAL_ENABLED}, local test mode)."
