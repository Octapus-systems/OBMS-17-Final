# Build OBMS from this repo's source (Odoo 17 core with our branding edits
# plus custom/ addons) rather than the stock odoo image, because the core
# templates, webclient.js and brand assets are modified here.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libxml2-dev \
        libxslt1-dev \
        libldap2-dev \
        libsasl2-dev \
        libjpeg-dev \
        zlib1g-dev \
        libfreetype6-dev \
        liblcms2-dev \
        libwebp-dev \
        libssl-dev \
        libffi-dev \
        node-less \
        npm \
        curl \
        xz-utils \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# wkhtmltopdf with patched Qt - required for correct PDF report rendering.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    curl -sSL -o /tmp/wkhtml.deb \
      "https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_${arch}.deb"; \
    apt-get update; \
    apt-get install -y --no-install-recommends /tmp/wkhtml.deb; \
    rm -f /tmp/wkhtml.deb; \
    rm -rf /var/lib/apt/lists/*

RUN useradd -ms /bin/bash odoo

WORKDIR /opt/odoo

# Dependencies first so source edits don't bust the pip layer.
COPY requirements.txt requirements-custom.txt ./
# setuptools must stay below 81: that release dropped pkg_resources, which
# gevent 22.10.2 imports at monkey-patch time. Without the pin the gevent
# worker dies on startup, port 8072 never opens, and every /websocket request
# 502s - i.e. Discuss realtime is silently dead while HTTP looks fine.
RUN pip install --upgrade pip wheel "setuptools<81" \
    && pip install -r requirements.txt -r requirements-custom.txt

COPY --chown=odoo:odoo . /opt/odoo

RUN mkdir -p /var/lib/odoo /etc/odoo && chown -R odoo:odoo /var/lib/odoo /etc/odoo

USER odoo
EXPOSE 8069

CMD ["python", "odoo-bin", "-c", "/etc/odoo/odoo.conf"]
