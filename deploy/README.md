# OBMS deployment

Docker Compose stack: Odoo 17 built from this repo, PostgreSQL 16, nginx.

## Live

| | |
|---|---|
| Instance | `i-0b8f253f91c5224f9` (`obms-odoo17`), `c7i-flex.large`, ap-south-1 |
| URL | http://3.111.245.98 |
| SSH | `ssh -i ~/.ssh/obms-deploy.pem ubuntu@3.111.245.98` |
| Source | `/opt/obms/src` |
| Stack | `/opt/obms/src/deploy` |

## Why the image is built from source

The Odoo core in this repo is patched: OBMS tab title, Octapus favicon and PWA
icons, "Powered by Octapus", Octapus Bot, and the Odoo.com links stripped from
the user menu. The stock `odoo:17` image would overwrite all of it.

## First deploy

```bash
# on the server
cd /opt/obms/src/deploy
cp ../../deploy.env .env            # DB_PASSWORD, ADMIN_PASSWD
docker compose build
docker compose up -d

# create the database with the same module set as the dev box
docker compose run --rm --no-deps odoo python odoo-bin \
  -c /etc/odoo/odoo.conf -d obms_17 \
  -i account,calendar,contacts,crm,purchase,sale_management,stock,\
eh_account_suite,eh_account_dynamic_reports,blue_web_theme \
  --stop-after-init --without-demo=all --workers=0 --max-cron-threads=0
```

## Redeploy (update code)

Push updated source to the running EC2 instance and rebuild containers.
The PostgreSQL data volume is **preserved** — only the Odoo app container is rebuilt.

```powershell
# From your Windows dev machine (uses the default key + host from the script):
.\deploy\redeploy.ps1

# With a custom key or different IP:
.\deploy\redeploy.ps1 -KeyPath "C:\keys\obms-deploy.pem" -Host "1.2.3.4"
```

What it does:

1. Packages the repo into a tarball (excluding `.git`, `venv`, `node_modules`, `filestore`)
2. Uploads via SCP to `/opt/obms/` on the EC2 instance
3. SSHs in, extracts, wires credentials from `deploy.env`, rebuilds the Odoo Docker image
4. Restarts only the `odoo` and `nginx` containers (`db` stays untouched)

**Prerequisites:**
- SSH key at `~/.ssh/obms-deploy.pem` (or pass `-KeyPath`)
- `deploy.env` must already exist at `/opt/obms/deploy.env` on the server (from first deploy)
- Windows 10+ (ships with `tar` and `ssh`)

## Gotchas that cost time

**`db_password` must be in `odoo.conf`.** This image runs `odoo-bin` directly,
so it does not inherit the stock odoo image's entrypoint that converts the
`HOST`/`USER`/`PASSWORD` env vars into CLI flags. Setting `PASSWORD` alone
gives `fe_sendauth: no password supplied`.

**`/websocket` must proxy to port 8072, not 8069.** With `workers > 0`, Odoo
serves websockets from a separate gevent process on `gevent_port`. Pointing
nginx at 8069 makes Discuss realtime silently dead in production while it
works fine in a threaded dev setup.

**Free-tier account.** This AWS account is on the Free plan, which refuses
non-eligible instance types (`t3.medium` was rejected). Eligible with 4 GiB:
`c7i-flex.large`.

## Still to do

**TLS.** Currently HTTP only, because no domain exists yet. Until a domain
points here and `nginx.conf` (with certbot) replaces `nginx-http.conf`,
Chrome treats the origin as insecure and blocks `getUserMedia` and the
Notification API - so Discuss **calls and desktop notifications will not work**
for anyone except a browser running on the server itself.

To switch once DNS is set:

```bash
cd /opt/obms/src/deploy
docker compose run --rm --entrypoint certbot certbot certonly \
  --webroot -w /var/www/certbot -d obms.example.com --agree-tos -m you@example.com
sed -i 's|nginx-http.conf|nginx.conf|' docker-compose.yml
sed -i 's|${DOMAIN}|obms.example.com|g' nginx.conf
docker compose up -d nginx
```

**STUN/TURN.** `mail.ice.server` is empty, so WebRTC only connects on the same
LAN. Seed public STUN with `ice_servers.py`; restrictive NATs additionally need
TURN (coturn, or Twilio under Settings > General Settings > Discuss).
