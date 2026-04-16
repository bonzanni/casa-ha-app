#!/command/with-contenv bashio

INGRESS_PORT=$(bashio::addon.ingress_port)

# Base nginx config
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

        # Casa API (aiohttp)
        location / {
            proxy_pass http://127.0.0.1:8099;
            proxy_http_version 1.1;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_read_timeout 300;
        }

        # WebSocket support for future streaming
        location /ws {
            proxy_pass http://127.0.0.1:8099/ws;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_read_timeout 86400;
        }
NGINX

# Conditionally add ttyd terminal location
if bashio::config.true 'enable_terminal'; then
    cat >> /etc/nginx/nginx.conf <<NGINX
        # ttyd web terminal
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

# Close server and http blocks
cat >> /etc/nginx/nginx.conf <<NGINX
    }
}
NGINX

bashio::log.info "Nginx configured (terminal: $(bashio::config 'enable_terminal'))"
