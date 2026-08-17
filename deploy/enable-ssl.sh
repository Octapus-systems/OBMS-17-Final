#!/bin/bash
set -eux
cd /opt/obms/src/deploy

# Replace DOMAIN in nginx.conf
sed -i 's/${DOMAIN}/obms.octapus.ae/g' nginx.conf

# Switch docker-compose.yml back to nginx.conf
sed -i 's|nginx-http.conf:/etc/nginx/conf.d/default.conf|nginx.conf:/etc/nginx/conf.d/default.conf|' docker-compose.yml

# Restart nginx
docker compose up -d nginx

echo ">>> SSL configured and Nginx restarted"
