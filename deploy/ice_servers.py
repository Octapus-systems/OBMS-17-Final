"""Seed STUN servers so Discuss calls can traverse NAT.

With zero mail.ice.server rows, WebRTC only ever connects between peers on the
same LAN. Public STUN lets each peer discover its external address.

STUN alone is not enough for symmetric NAT / restrictive corporate firewalls -
that needs TURN (coturn, or Twilio via Settings > General Settings > Discuss).

Run: docker compose exec odoo python odoo-bin shell -c /etc/odoo/odoo.conf \
         -d obms_17 --no-http < deploy/ice_servers.py
"""
# host:port ONLY - no "stun:" prefix. mail.ice.server._get_local_ice_servers()
# builds the URL as '%s:%s' % (server_type, uri), so including the scheme here
# yields "stun:stun:host:port" and the browser throws
#   SyntaxError: Failed to construct 'RTCPeerConnection': ICE server parsing
#   failed: Invalid port
STUN = [
    "stun.l.google.com:19302",
    "stun1.l.google.com:19302",
    "stun2.l.google.com:19302",
]

Ice = env["mail.ice.server"].sudo()
for uri in STUN:
    if not Ice.search([("uri", "=", uri)], limit=1):
        Ice.create({"server_type": "stun", "uri": uri})
        print("added", uri)
    else:
        print("exists", uri)

env.cr.commit()
print("total ice servers:", Ice.search_count([]))
