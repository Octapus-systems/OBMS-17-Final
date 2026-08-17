#!/bin/bash
set -eux
cd /opt/obms

# Wire credentials from deploy.env
if [ -f deploy.env ]; then
    DB_PASSWORD=$(grep '^DB_PASSWORD=' deploy.env | cut -d= -f2)
    ADMIN_PASSWD=$(grep '^ADMIN_PASSWD=' deploy.env | cut -d= -f2)
    sed -i "s/^db_password = .*/db_password = ${DB_PASSWORD}/" src/deploy/odoo.conf
    sed -i "s/^admin_passwd = .*/admin_passwd = ${ADMIN_PASSWD}/" src/deploy/odoo.conf
    echo ">>> Credentials wired"
else
    echo "WARNING: deploy.env not found"
fi

cd src/deploy

# Rebuild only the odoo image
echo '>>> Building Odoo image...'
docker compose build odoo 2>&1 | tail -30

# Recreate containers
echo '>>> Restarting containers...'
docker compose up -d --force-recreate --no-deps odoo
docker compose up -d nginx

# Cleanup
rm -rf /opt/obms/src.old

echo '>>> Waiting for health...'
sleep 10
docker compose ps
echo '>>> Deploy complete!'
