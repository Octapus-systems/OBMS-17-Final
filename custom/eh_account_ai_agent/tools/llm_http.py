# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Minimal HTTP helper for LLM provider extension modules.

Uses only the Python standard library (urllib) so provider modules add
no third-party dependency. Provider adapters call http_post_json to
POST a JSON body and parse a JSON response, translating every transport
or decode failure into a ProviderError the capability layer already
knows how to swallow.

Tests monkeypatch http_post_json so the real network is never touched
in CI.
"""

import ipaddress
import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .provider_registry import ProviderError


def _validate_url(url):
    """SSRF guard for admin-configured LLM endpoints.

    Requires an http(s) URL and refuses link-local / cloud-metadata targets
    (169.254.0.0/16, fe80::/10, metadata.google.internal), the classic SSRF
    pivot to steal instance credentials. Local providers using 127.0.0.1 are
    still allowed on purpose. Note: a hostname that resolves to a blocked
    range at connect time (DNS rebinding) is out of scope for this literal
    check; the metadata IP and link-local literals are the practical vector.
    """
    parsed = urlparse(url or '')
    if parsed.scheme not in ('http', 'https'):
        raise ProviderError("LLM endpoint must be an http(s) URL.")
    host = parsed.hostname or ''
    if not host:
        raise ProviderError("LLM endpoint URL has no host.")
    if host == 'metadata.google.internal':
        raise ProviderError(
            "LLM endpoint may not target the cloud metadata service.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_link_local or ip.is_multicast
                           or ip.is_reserved):
        raise ProviderError(
            "LLM endpoint may not target link-local or reserved addresses.")


def http_post_json(url, payload, headers=None, timeout=30):
    """POST `payload` as JSON to `url`; return the parsed JSON response.

    :param url: absolute endpoint URL.
    :param payload: dict serialised to a JSON request body.
    :param headers: optional dict of extra request headers.
    :param timeout: socket timeout in seconds.
    :raises ProviderError: on any connection, HTTP, or decode error.
    """
    _validate_url(url)
    body = json.dumps(payload).encode('utf-8')
    req_headers = {'Content-Type': 'application/json'}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(
        url, data=body, headers=req_headers, method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = exc.read().decode('utf-8')[:500]
        except Exception:  # noqa: BLE001 - best-effort detail only
            detail = ''
        raise ProviderError(
            "LLM endpoint returned HTTP %s: %s" % (exc.code, detail),
        )
    except urllib.error.URLError as exc:
        raise ProviderError(
            "Could not reach LLM endpoint %s: %s" % (url, exc.reason),
        )
    except Exception as exc:  # noqa: BLE001 - never leak a raw error
        raise ProviderError("LLM request failed: %s" % exc)
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            "LLM endpoint returned non-JSON response: %s" % exc,
        )
