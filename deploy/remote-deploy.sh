#!/bin/bash
# Unpack the uploaded source, wire credentials, build and start OBMS.
set -eux

cd /opt/obms

if [ -f obms.tar.gz ]; then
    mkdir -p src
    tar -xzf obms.tar.gz -C src
    rm -f obms.tar.gz
fi

cd /opt/obms/src

# Credentials generated on the deploy machine.
cp /opt/obms/deploy.env deploy/.env
DB_PASSWORD=$(grep '^DB_PASSWORD=' deploy/.env | cut -d= -f2)
ADMIN_PASSWD=$(grep '^ADMIN_PASSWD=' deploy/.env | cut -d= -f2)

# The container config ships with placeholder credentials.
sed -i "s|^admin_passwd = .*|admin_passwd = ${ADMIN_PASSWD}|" deploy/odoo.conf
sed -i "s|^db_password = .*|db_password = ${DB_PASSWORD}|" deploy/odoo.conf

# No domain yet, so serve plain HTTP and drop the certbot/TLS services.
sed -i 's|./nginx.conf:/etc/nginx/conf.d/default.conf:ro|./nginx-http.conf:/etc/nginx/conf.d/default.conf:ro|' deploy/docker-compose.yml
python3 - <<'PY'
import re
p = "deploy/docker-compose.yml"
s = open(p).read()
# Remove the certbot service block and its TLS-only volume mounts; without a
# certificate nginx would fail to start on the 443 server block.
s = re.sub(r"\n  certbot:\n(?:.*\n)*?(?=\nvolumes:)", "\n", s)
s = s.replace("      - certbot-conf:/etc/letsencrypt:ro\n", "")
s = s.replace("      - certbot-www:/var/www/certbot:ro\n", "")
s = s.replace("  certbot-conf:\n", "")
s = s.replace("  certbot-www:\n", "")
open(p, "w").write(s)
print(s)
PY

cd deploy
docker compose build 2>&1 | tail -20
docker compose up -d
sleep 10
docker compose ps
